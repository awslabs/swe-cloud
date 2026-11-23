"""Verifier-side Kafka proxy: relays writer traffic to the broker and imposes the fault schedule.

Each writer process gets its own listener (``A``, ``B``) so the proxy knows who sent a request without
trusting anything the writer says about itself. Metadata and FindCoordinator responses are rewritten
so the writer keeps talking through its own listener.

Schedules (all optional, configured before the writers start):

- ``hold``: the first Produce request from ``hold_listener`` whose bytes contain ``hold_match`` is
  kept and never answered; that client connection is black-holed. The kept bytes are replayed to the
  broker on a fresh connection when the next successful AddPartitionsToTxn response for the topic is
  about to be relayed to any client (the response is relayed only after the replay is answered), or
  ``release_fallback_s`` after the hold.
- ``takeover``: from the request on listener A selected by ``takeover_select`` on, A's transactional
  requests are held, and ``takeover_needed`` is set so the driver starts the second writer on B.
  The held requests are forwarded once B's first successful EndTxn(COMMIT) has been relayed (or after
  ``takeover_max_hold_s``), and B's later requests wait until one of them has been answered (at most
  ``takeover_gate_s``). With ``takeover_release_inside`` they are forwarded instead at the first
  successful Produce response B gets after that commit, so A's recovery reaches the coordinator while
  B has a transaction open; otherwise it lands between two of B's transactions.
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time

PRODUCE, FETCH, LIST_OFFSETS, METADATA, FIND_COORDINATOR = 0, 1, 2, 3, 10
API_VERSIONS, INIT_PRODUCER_ID, ADD_PARTITIONS_TO_TXN, END_TXN = 18, 22, 24, 26
TXN_APIS = {PRODUCE, INIT_PRODUCER_ID, ADD_PARTITIONS_TO_TXN, END_TXN}
# Admin APIs the writer's principal is denied (OPERATIONS.md): topic/config administration, transaction
# administration and inspection.
DENIED = {19: "CreateTopics", 20: "DeleteTopics", 21: "DeleteRecords", 27: "WriteTxnMarkers", 33: "AlterConfigs",
          37: "CreatePartitions", 44: "IncrementalAlterConfigs", 60: "DescribeCluster", 61: "DescribeProducers",
          65: "DescribeTransactions", 66: "ListTransactions"}
# first flexible version of each api (request header v2, response header v1 except ApiVersions)
FLEX = {PRODUCE: 9, FETCH: 12, LIST_OFFSETS: 6, METADATA: 9, FIND_COORDINATOR: 3, API_VERSIONS: 3,
        INIT_PRODUCER_ID: 2, ADD_PARTITIONS_TO_TXN: 3, END_TXN: 3}
NAMES = {PRODUCE: "Produce", FETCH: "Fetch", LIST_OFFSETS: "ListOffsets", METADATA: "Metadata",
         FIND_COORDINATOR: "FindCoordinator", API_VERSIONS: "ApiVersions", INIT_PRODUCER_ID: "InitProducerId",
         ADD_PARTITIONS_TO_TXN: "AddPartitionsToTxn", END_TXN: "EndTxn"}


def flexible(api: int, version: int) -> bool:
    return api in FLEX and version >= FLEX[api]


class R:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos

    def take(self, fmt: str):
        size = struct.calcsize(fmt)
        (v,) = struct.unpack_from(fmt, self.d, self.p)
        self.p += size
        return v

    def i8(self): return self.take(">b")
    def i16(self): return self.take(">h")
    def i32(self): return self.take(">i")
    def i64(self): return self.take(">q")

    def uvarint(self):
        shift = out = 0
        while True:
            b = self.d[self.p]
            self.p += 1
            out |= (b & 0x7F) << shift
            if not b & 0x80:
                return out
            shift += 7

    def string(self):
        n = self.i16()
        if n < 0:
            return None
        s = self.d[self.p:self.p + n].decode()
        self.p += n
        return s

    def cstring(self):
        n = self.uvarint()
        if n == 0:
            return None
        s = self.d[self.p:self.p + n - 1].decode()
        self.p += n - 1
        return s

    def tags(self):
        for _ in range(self.uvarint()):
            self.uvarint()
            self.p += self.uvarint()

    def str_(self, flex: bool):
        return self.cstring() if flex else self.string()

    def count(self, flex: bool):
        return self.uvarint() - 1 if flex else self.i32()


def _uvarint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _enc_str(s: str, flex: bool) -> bytes:
    raw = s.encode()
    return (_uvarint(len(raw) + 1) if flex else struct.pack(">h", len(raw))) + raw


def parse_request_header(frame: bytes):
    r = R(frame)
    api, version, corr = r.i16(), r.i16(), r.i32()
    client_id = r.string()
    if flexible(api, version):
        r.tags()
    return api, version, corr, client_id, r.p


def parse_request_body(api: int, version: int, frame: bytes, pos: int) -> dict:
    flex = flexible(api, version)
    r = R(frame, pos)
    if api == INIT_PRODUCER_ID:
        out = {"transactional_id": r.str_(flex), "transaction_timeout_ms": r.i32()}
        if version >= 3:
            out["producer_id"], out["producer_epoch"] = r.i64(), r.i16()
        else:
            out["producer_id"], out["producer_epoch"] = -1, -1
        return out
    if api == ADD_PARTITIONS_TO_TXN:
        if version >= 4:
            return {"unsupported": True}
        out = {"transactional_id": r.str_(flex), "producer_id": r.i64(), "producer_epoch": r.i16()}
        return out
    if api == END_TXN:
        return {"transactional_id": r.str_(flex), "producer_id": r.i64(), "producer_epoch": r.i16(),
                "committed": r.i8() != 0}
    if api == PRODUCE:
        if version < 3:
            return {"transactional_id": None}
        return {"transactional_id": r.str_(flex)}
    return {}


def parse_response(api: int, version: int, body: bytes, topic: str) -> dict:
    """``body`` starts after the response header."""
    flex = flexible(api, version)
    r = R(body)
    if api == INIT_PRODUCER_ID:
        r.i32()
        return {"error": r.i16(), "producer_id": r.i64(), "producer_epoch": r.i16()}
    if api == END_TXN:
        r.i32()
        return {"error": r.i16()}
    if api == ADD_PARTITIONS_TO_TXN:
        r.i32()
        errors = {}
        for _ in range(r.count(flex)):
            name = r.str_(flex)
            for _ in range(r.count(flex)):
                part, err = r.i32(), r.i16()
                if flex:
                    r.tags()
                errors[(name, part)] = err
            if flex:
                r.tags()
        return {"error": errors.get((topic, 0), errors and max(errors.values()) or 0), "errors": str(errors)}
    if api == PRODUCE:
        err = None
        for _ in range(r.count(flex)):
            name = r.str_(flex)
            for _ in range(r.count(flex)):
                part, e = r.i32(), r.i16()
                if name == topic and part == 0 and err is None:
                    err = e
                break
            break
        return {"error": err}
    return {}


def rewrite_addresses(api: int, version: int, body: bytes, host: str, port: int) -> bytes:
    flex = flexible(api, version)
    r = R(body)
    out = bytearray()
    if api == METADATA:
        if version >= 3:
            r.i32()
        out += body[:r.p]
        n = r.count(flex)
        out += _uvarint(n + 1) if flex else struct.pack(">i", n)
        for _ in range(n):
            node = r.i32()
            r.str_(flex)
            r.i32()
            start = r.p
            if version >= 1:
                r.str_(flex)  # rack
            if flex:
                r.tags()
            out += struct.pack(">i", node) + _enc_str(host, flex) + struct.pack(">i", port) + body[start:r.p]
        out += body[r.p:]
        return bytes(out)
    if api == FIND_COORDINATOR:
        if version >= 4:
            r.i32()
            out += body[:r.p]
            n = r.count(True)
            out += _uvarint(n + 1)
            for _ in range(n):
                key_start = r.p
                r.cstring()
                node = r.i32()
                out += body[key_start:r.p - 4] + struct.pack(">i", node)
                r.cstring()
                r.i32()
                out += _enc_str(host, True) + struct.pack(">i", port)
                start = r.p
                r.i16()
                r.cstring()
                r.tags()
                out += body[start:r.p]
            out += body[r.p:]
            return bytes(out)
        if version >= 1:
            r.i32()
        r.i16()
        if version >= 1:
            r.str_(flex)
        node = r.i32()
        out += body[:r.p - 4] + struct.pack(">i", node)
        r.str_(flex)
        r.i32()
        out += _enc_str(host, flex) + struct.pack(">i", port) + body[r.p:]
        return bytes(out)
    return body


async def _read_frame(reader: asyncio.StreamReader) -> bytes | None:
    try:
        size = struct.unpack(">i", await reader.readexactly(4))[0]
        return await reader.readexactly(size)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        return None


def _write_frame(writer: asyncio.StreamWriter, frame: bytes) -> None:
    writer.write(struct.pack(">i", len(frame)) + frame)


class Proxy:
    def __init__(self, upstream: tuple[str, int], topic: str, txn_id: str, listeners: dict[str, int],
                 transaction_timeout_ms: int = 60000):
        self.upstream = upstream
        self.topic = topic
        self.txn_id = txn_id
        self.listeners = listeners
        self.transaction_timeout_ms = transaction_timeout_ms
        self.t0 = time.monotonic()
        self.events: list[dict] = []
        self.violations: list[str] = []
        self._lock = threading.Lock()
        # hold schedule
        self.hold_listener: str | None = None
        self.hold_match: bytes | None = None
        self.release_fallback_s = 20.0
        self.hold_time: float | None = None
        self.replay_result: dict | None = None
        self._held_produce: bytes | None = None
        self._replay_started = False
        # takeover schedule
        self.takeover_select = None  # callable(proxy, api, info) -> bool, for listener A requests
        self.takeover_max_hold_s = 15.0
        self.takeover_gate_s = 5.0
        self.takeover_release_inside = False
        self._b_committed = False
        self.takeover_needed = threading.Event()
        self.takeover_phase = "idle"  # idle -> held -> released -> answered
        self.takeover_events: list[dict] = []
        self._add_partitions_from_a = 0
        self._takeover_deadline = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers = []
        self._ready = threading.Event()

    # ── lifecycle ──
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("proxy did not start")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._released = asyncio.Event()
        self._answered = asyncio.Event()
        self._replayed = asyncio.Event()
        for name, port in self.listeners.items():
            server = self._loop.run_until_complete(asyncio.start_server(
                lambda r, w, n=name: self._client(n, r, w), "127.0.0.1", port))
            self._servers.append(server)
        self._ready.set()
        self._loop.run_forever()

    def stop(self) -> None:
        if self._loop is None:
            return
        def _shutdown():
            for s in self._servers:
                s.close()
            for task in asyncio.all_tasks(self._loop):
                task.cancel()
            self._loop.call_soon(self._loop.stop)
        self._loop.call_soon_threadsafe(_shutdown)
        if self._thread:
            self._thread.join(5)

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def _event(self, **kw) -> dict:
        kw["t"] = self.now()
        with self._lock:
            self.events.append(kw)
        return kw

    def _violation(self, text: str) -> None:
        with self._lock:
            self.violations.append(text)

    # ── replay of the held produce ──
    async def _replay(self, reason: str) -> None:
        if self._replay_started or self._held_produce is None:
            return
        self._replay_started = True
        frame = self._held_produce
        api, version, corr, _, _ = parse_request_header(frame)
        result = {"reason": reason, "t": self.now(), "error": None}
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(*self.upstream), 5)
            _write_frame(writer, frame)
            await writer.drain()
            resp = await asyncio.wait_for(_read_frame(reader), 10)
            writer.close()
            if resp is not None:
                rr = R(resp)
                rr.i32()
                if flexible(api, version):
                    rr.tags()
                result["error"] = parse_response(PRODUCE, version, resp[rr.p:], self.topic)["error"]
        except Exception as exc:  # noqa: BLE001 - evidence only
            result["exception"] = repr(exc)
        result["answered_t"] = self.now()
        self.replay_result = result
        self._event(kind="replay", **result)
        self._replayed.set()

    async def _fallback_replay(self) -> None:
        await asyncio.sleep(self.release_fallback_s)
        await self._replay("fallback")

    # ── per connection ──
    async def _client(self, listener: str, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        port = self.listeners[listener]
        try:
            ureader, uwriter = await asyncio.wait_for(asyncio.open_connection(*self.upstream), 5)
        except Exception:  # noqa: BLE001
            cwriter.close()
            return
        conn = {"pending": {}, "blackholed": False, "closed": False}

        async def upstream_to_client():
            try:
                await relay()
            except Exception as exc:  # noqa: BLE001 - a proxy defect must be visible, not a silent stall
                self._violation(f"proxy internal error on {listener}: {exc!r}")
                self._event(kind="proxy_error", listener=listener, error=repr(exc))
            conn["closed"] = True
            cwriter.close()

        async def relay():
            while True:
                frame = await _read_frame(ureader)
                if frame is None:
                    break
                corr = struct.unpack_from(">i", frame, 0)[0]
                api, version, info = conn["pending"].pop(corr, (None, 0, {}))
                if api is None:
                    continue
                pos = 4
                if api != API_VERSIONS and flexible(api, version):
                    rr = R(frame, 4)
                    rr.tags()
                    pos = rr.p
                body = frame[pos:]
                if api in (METADATA, FIND_COORDINATOR):
                    try:
                        body = rewrite_addresses(api, version, body, "127.0.0.1", port)
                    except Exception as exc:  # noqa: BLE001
                        self._violation(f"{listener}: cannot rewrite {NAMES[api]} v{version}: {exc!r}")
                        break
                    frame = frame[:pos] + body
                elif api in (INIT_PRODUCER_ID, END_TXN, ADD_PARTITIONS_TO_TXN, PRODUCE):
                    try:
                        parsed = parse_response(api, version, body, self.topic)
                    except Exception:  # noqa: BLE001
                        parsed = {}
                    ev = self._event(kind="response", listener=listener, api=NAMES[api], req=info, **parsed)
                    await self._on_response(listener, api, info, parsed, ev)
                if conn["blackholed"]:
                    continue
                _write_frame(cwriter, frame)
                await cwriter.drain()

        async def forward(api, version, corr, info, frame):
            conn["pending"][corr] = (api, version, info)
            _write_frame(uwriter, frame)
            await uwriter.drain()

        pump = asyncio.ensure_future(upstream_to_client())
        try:
            while True:
                frame = await _read_frame(creader)
                if frame is None:
                    break
                if conn["blackholed"]:
                    continue
                try:
                    api, version, corr, client_id, pos = parse_request_header(frame)
                except Exception:  # noqa: BLE001
                    self._violation(f"{listener}: unparseable request header")
                    break
                if api in DENIED:
                    self._violation(f"{listener}: {DENIED[api]} (api {api}) is denied to the writer's principal")
                    break
                try:
                    info = parse_request_body(api, version, frame, pos)
                except Exception:  # noqa: BLE001
                    info = {}
                if info.get("unsupported"):
                    self._violation(f"{listener}: {NAMES[api]} v{version} not supported by the proxy")
                    break
                if not self._allowed(listener, api, version, info):
                    break
                if api in (INIT_PRODUCER_ID, END_TXN, ADD_PARTITIONS_TO_TXN, PRODUCE):
                    self._event(kind="request", listener=listener, api=NAMES[api], version=version, **info)
                # the produce hold
                if (api == PRODUCE and listener == self.hold_listener and self.hold_match is not None
                        and self._held_produce is None and self.hold_match in frame):
                    self._held_produce = frame
                    self.hold_time = self.now()
                    conn["blackholed"] = True
                    self._event(kind="hold", listener=listener)
                    asyncio.ensure_future(self._fallback_replay())
                    continue
                if api == ADD_PARTITIONS_TO_TXN and listener == "A":
                    self._add_partitions_from_a += 1
                # the takeover hold on A: once selected, every transactional request from A waits
                if (listener == "A" and self.takeover_select is not None and self.takeover_phase == "idle"
                        and self.takeover_select(self, api, info)):
                    self.takeover_phase = "held"
                    self._takeover_deadline = time.monotonic() + self.takeover_max_hold_s
                    self._event(kind="takeover_hold", listener=listener, api=NAMES[api], req=info)
                    self.takeover_needed.set()
                if listener == "A" and self.takeover_phase == "held" and api in TXN_APIS:
                    try:
                        await asyncio.wait_for(self._released.wait(),
                                               max(0.0, self._takeover_deadline - time.monotonic()))
                    except asyncio.TimeoutError:
                        if self.takeover_phase == "held":
                            self._event(kind="takeover_release_timeout")
                            self.takeover_phase = "released"
                            self._released.set()
                    if conn["closed"]:
                        self._event(kind="takeover_held_connection_gone", api=NAMES[api])
                        break
                    self._event(kind="takeover_forward", api=NAMES[api])
                    await forward(api, version, corr, dict(info, takeover_held=True), frame)
                    continue
                # B waits while A's released request is in flight
                if listener == "B" and self.takeover_phase == "released":
                    try:
                        await asyncio.wait_for(self._answered.wait(), self.takeover_gate_s)
                    except asyncio.TimeoutError:
                        self._event(kind="takeover_gate_timeout")
                await forward(api, version, corr, info, frame)
        finally:
            pump.cancel()
            uwriter.close()
            cwriter.close()

    def _allowed(self, listener: str, api: int, version: int, info: dict) -> bool:
        if api in (INIT_PRODUCER_ID, ADD_PARTITIONS_TO_TXN, END_TXN):
            if info.get("transactional_id") != self.txn_id:
                self._violation(f"{listener}: {NAMES[api]} for transactional id {info.get('transactional_id')!r}")
                return False
        if api == INIT_PRODUCER_ID and info.get("transaction_timeout_ms") != self.transaction_timeout_ms:
            self._violation(f"{listener}: InitProducerId with transaction_timeout_ms={info.get('transaction_timeout_ms')}")
            return False
        if api == PRODUCE and info.get("transactional_id") != self.txn_id:
            self._violation(f"{listener}: Produce with transactional id {info.get('transactional_id')!r}")
            return False
        return True

    async def _on_response(self, listener: str, api: int, info: dict, parsed: dict, ev: dict) -> None:
        if (api == ADD_PARTITIONS_TO_TXN and parsed.get("error") == 0 and self._held_produce is not None
                and not self._replay_started):
            await self._replay("add_partitions")
        if api == END_TXN and listener == "B" and info.get("committed") and parsed.get("error") == 0:
            if self.takeover_phase == "held" and not self.takeover_release_inside:
                self._release_takeover()
            self._b_committed = True
        if (api == PRODUCE and listener == "B" and parsed.get("error") == 0 and self._b_committed
                and self.takeover_release_inside and self.takeover_phase == "held"):
            self._release_takeover()
        if listener == "A" and info.get("takeover_held"):
            self.takeover_phase = "answered"
            self._answered.set()

    def _release_takeover(self) -> None:
        self.takeover_phase = "released"
        self._event(kind="takeover_release")
        self._released.set()

    # ── views for the verifier ──
    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.events)

    def add_partitions_from_a(self) -> int:
        return self._add_partitions_from_a
