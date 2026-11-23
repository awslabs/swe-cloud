"""Verifier harness: the MSK cluster, the topics, the writer processes and an independent view of the ledger.

Nothing here imports from /app. The ledger is read with librdkafka (confluent-kafka) directly from the
broker, never through the proxy.
"""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

import boto3
from botocore.config import Config
from confluent_kafka import Consumer, IsolationLevel, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic, OffsetSpec

sys.path.insert(0, os.path.dirname(__file__))
from kproxy import ADD_PARTITIONS_TO_TXN, INIT_PRODUCER_ID, Proxy  # noqa: E402

WRITER = "/app/ledger_writer.py"
TRANSACTION_TIMEOUT_MS = 60000
DELIVERY_TIMEOUT_S = 5.0
RECOVERY_BUDGET_S = 15.0
PROCESS_DEADLINE_S = 90.0
RUN_DEADLINE_S = 60.0


class EnvironmentNotLive(RuntimeError):
    pass


def ensure_cluster() -> str:
    kafka = boto3.client("kafka", config=Config(read_timeout=600, retries={"max_attempts": 1}))
    try:
        # Clusters left from the working session are not used; they hold broker resources the engine
        # host has little of, so remove them before starting the verifier's own.
        for cluster in kafka.list_clusters_v2().get("ClusterInfoList", []):
            print(f"removing leftover MSK cluster {cluster['ClusterName']} ({cluster['State']})")
            kafka.delete_cluster(ClusterArn=cluster["ClusterArn"])
        time.sleep(2)
    except Exception as exc:  # noqa: BLE001
        raise EnvironmentNotLive(f"MSK ListClusters/DeleteCluster failed: {exc!r}") from exc
    bootstrap = None
    for attempt in range(3):
        name = "verify-" + uuid.uuid4().hex[:8]
        try:
            arn = kafka.create_cluster(
                ClusterName=name, KafkaVersion="3.6.0", NumberOfBrokerNodes=1,
                BrokerNodeGroupInfo={"InstanceType": "kafka.m5.large", "ClientSubnets": ["subnet-verify"]},
            )["ClusterArn"]
            deadline = time.monotonic() + (240 if attempt == 0 else 120)
            while kafka.describe_cluster(ClusterArn=arn)["ClusterInfo"]["State"] != "ACTIVE":
                if time.monotonic() > deadline:
                    break
                time.sleep(1)
            else:
                bootstrap = kafka.get_bootstrap_brokers(ClusterArn=arn)["BootstrapBrokerString"]
                break
            print(f"MSK cluster {name} not ACTIVE in time; deleting and retrying")
            kafka.delete_cluster(ClusterArn=arn)
            time.sleep(5)
        except Exception as exc:  # noqa: BLE001
            raise EnvironmentNotLive(f"MSK CreateCluster failed: {exc!r}") from exc
    if bootstrap is None:
        raise EnvironmentNotLive("MSK cluster never became ACTIVE")
    host, port = bootstrap.split(",")[0].rsplit(":", 1)
    deadline = time.monotonic() + 60
    while True:
        try:
            socket.create_connection((host, int(port)), timeout=2).close()
            break
        except OSError:
            if time.monotonic() > deadline:
                raise EnvironmentNotLive(f"broker {bootstrap} not reachable")
            time.sleep(1)
    admin = AdminClient({"bootstrap.servers": bootstrap})
    deadline = time.monotonic() + 60
    while True:
        try:
            admin.list_topics(timeout=5)
            return bootstrap
        except KafkaException:
            if time.monotonic() > deadline:
                raise EnvironmentNotLive(f"broker {bootstrap} does not answer metadata")
            time.sleep(1)


def create_topic(bootstrap: str, topic: str) -> None:
    admin = AdminClient({"bootstrap.servers": bootstrap})
    admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])[topic].result(30)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        md = admin.list_topics(topic, timeout=5).topics.get(topic)
        if md is not None and md.error is None and 0 in md.partitions and md.partitions[0].leader >= 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"topic {topic} has no leader")


def make_batches(rng: random.Random, count: int) -> list[dict]:
    day = f"2026{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}"
    start = rng.randint(100, 8000)
    tag = uuid.uuid4().hex[:4].upper()
    out = []
    for n in range(start, start + count):
        bid = f"STL-{day}-{tag}{n:04d}"
        out.append({"batch": bid, "entries": [
            {"entry": f"{bid}-{i}", "account": f"ACC-{rng.randint(1000, 9999)}", "amount_cents": rng.randint(100, 900000)}
            for i in range(1, 4)]})
    return out


def entry_value(batch: dict, entry: dict, writer: str, attempt: int) -> bytes:
    return json.dumps({"batch": batch["batch"], **entry, "writer": writer, "attempt": attempt}, sort_keys=True).encode()


def seed_batches(bootstrap: str, topic: str, txn_id: str, batches: list[dict], writer: str) -> None:
    """Commit ``batches`` as an earlier generation of the writer would have, under the same transactional id."""
    if not batches:
        return
    producer = Producer({"bootstrap.servers": bootstrap, "transactional.id": txn_id,
                         "transaction.timeout.ms": TRANSACTION_TIMEOUT_MS, "compression.type": "none"})
    producer.init_transactions(30)
    for batch in batches:
        producer.begin_transaction()
        for entry in batch["entries"]:
            producer.produce(topic, key=entry["entry"].encode(), value=entry_value(batch, entry, writer, 1), partition=0)
        producer.commit_transaction(30)
    producer.flush(10)


def offsets(bootstrap: str, topic: str) -> tuple[int, int]:
    """(last stable offset, high watermark) of partition 0."""
    admin = AdminClient({"bootstrap.servers": bootstrap})
    tp = TopicPartition(topic, 0)
    lso = admin.list_offsets({tp: OffsetSpec.latest()}, isolation_level=IsolationLevel.READ_COMMITTED)[tp].result(10).offset
    hwm = admin.list_offsets({tp: OffsetSpec.latest()}, isolation_level=IsolationLevel.READ_UNCOMMITTED)[tp].result(10).offset
    return lso, hwm


def settle_offsets(bootstrap: str, topic: str, within_s: float = 5.0) -> tuple[int, int]:
    deadline = time.monotonic() + within_s
    while True:
        lso, hwm = offsets(bootstrap, topic)
        if lso == hwm or time.monotonic() > deadline:
            return lso, hwm
        time.sleep(0.25)


def read_committed(bootstrap: str, topic: str) -> list[dict]:
    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": "verify-" + uuid.uuid4().hex,
                         "isolation.level": "read_committed", "enable.auto.commit": False,
                         "auto.offset.reset": "earliest"})
    tp = TopicPartition(topic, 0, 0)
    consumer.assign([tp])
    lso, _ = offsets(bootstrap, topic)
    out: list[dict] = []
    deadline = time.monotonic() + 20
    idle_since = time.monotonic()
    try:
        while time.monotonic() < deadline:
            msg = consumer.poll(0.25)
            if msg is not None and not msg.error():
                idle_since = time.monotonic()
                try:
                    value = json.loads(msg.value())
                except (TypeError, ValueError):
                    value = {"unparseable": repr(msg.value())}
                value["_key"] = msg.key().decode() if msg.key() else None
                value["_offset"] = msg.offset()
                out.append(value)
                continue
            position = consumer.position([TopicPartition(topic, 0)])[0].offset
            if position >= lso and time.monotonic() - idle_since > 1.0:
                break
            if time.monotonic() - idle_since > 6.0:
                break
    finally:
        consumer.close()
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Writer:
    name: str
    listener: str
    proc: subprocess.Popen
    log_path: str
    started: float
    ended: float | None = None
    code: int | None = None

    def log(self) -> str:
        try:
            with open(self.log_path) as f:
                return f.read()[-6000:]
        except OSError:
            return ""


@dataclass
class Run:
    kind: str
    topic: str
    txn_id: str
    batches: list[dict]
    seeded: int
    hold_entry: str | None
    writers: dict[str, Writer] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    hold_time: float | None = None
    replay: dict | None = None
    committed: list[dict] = field(default_factory=list)
    lso: int = -1
    hwm: int = -1
    takeover_phase: str = ""

    def describe(self) -> str:
        lines = [f"run {self.kind}: topic={self.topic} txn_id={self.txn_id} seeded={self.seeded} hold={self.hold_entry}"]
        for w in self.writers.values():
            lines.append(f"  writer {w.name} on {w.listener}: exit={w.code} ran={None if w.ended is None else round(w.ended - w.started, 1)}s")
        lines.append(f"  hold_time={self.hold_time} replay={self.replay} takeover={self.takeover_phase}")
        lines.append(f"  violations={self.violations}")
        lines.append(f"  lso={self.lso} hwm={self.hwm}")
        lines.append("  proxy events:")
        for ev in self.events[-80:]:
            lines.append("    " + json.dumps(ev, default=str))
        for w in self.writers.values():
            lines.append(f"  ---- {w.name} log tail ----")
            lines.extend("    " + line for line in w.log().splitlines()[-40:])
        return "\n".join(lines)

    def expected_entries(self) -> Counter:
        return Counter((b["batch"], e["entry"], e["account"], e["amount_cents"]) for b in self.batches for e in b["entries"])

    def committed_entries(self) -> Counter:
        return Counter((v.get("batch"), v.get("entry"), v.get("account"), v.get("amount_cents")) for v in self.committed)

    def commit_times(self, listener: str) -> list[float]:
        return [ev["t"] for ev in self.events if ev.get("kind") == "response" and ev.get("api") == "EndTxn"
                and ev.get("listener") == listener and ev.get("req", {}).get("committed") and ev.get("error") == 0]


def _spawn(run: Run, name: str, listener: str, port: int, batches_file: str, workdir: str) -> Writer:
    log_path = os.path.join(workdir, f"{name}.log")
    cwd = tempfile.mkdtemp(prefix=f"{name}-")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env["PYTHONPATH"] = "/app"
    proc = subprocess.Popen(
        [sys.executable, WRITER, "--bootstrap", f"127.0.0.1:{port}", "--topic", run.topic, "--txn-id", run.txn_id,
         "--instance", name, "--batches", batches_file],
        cwd=cwd, env=env, stdout=open(log_path, "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    w = Writer(name, listener, proc, log_path, time.monotonic())
    run.writers[name] = w
    return w


def _poll(w: Writer) -> bool:
    if w.code is None:
        code = w.proc.poll()
        if code is not None:
            w.code, w.ended = code, time.monotonic()
    return w.code is not None


def execute(kind: str, bootstrap: str, seed: int) -> Run:
    """kind: control | timeout | timeout_takeover | timeout_takeover_inside | takeover"""
    rng = random.Random(seed)
    token = uuid.uuid4().hex[:6]
    seeded = rng.randint(1, 2)
    batches = make_batches(rng, seeded + rng.randint(5, 6))
    hold_entry = None
    if kind in ("timeout", "timeout_takeover", "timeout_takeover_inside"):
        hold_batch = batches[seeded + rng.randint(1, 2)]
        hold_entry = hold_batch["entries"][rng.randint(0, 2)]["entry"]
    run = Run(kind, f"ledger-{token}", f"ledger-writer-{token}", batches, seeded, hold_entry)
    create_topic(bootstrap, run.topic)
    seed_batches(bootstrap, run.topic, run.txn_id, batches[:seeded], f"ledger-writer-prev-{token}")

    workdir = tempfile.mkdtemp(prefix=f"run-{kind}-")
    batches_file = os.path.join(workdir, "batches.jsonl")
    with open(batches_file, "w") as f:
        for b in batches:
            f.write(json.dumps(b) + "\n")

    host, port = bootstrap.split(",")[0].rsplit(":", 1)
    ports = {"A": free_port(), "B": free_port()}
    proxy = Proxy((host, int(port)), run.topic, run.txn_id, ports, TRANSACTION_TIMEOUT_MS)
    if hold_entry:
        proxy.hold_listener = "A"
        proxy.hold_match = hold_entry.encode()
    takeover_at_add = rng.randint(2, 3)
    if kind in ("timeout_takeover", "timeout_takeover_inside"):
        proxy.takeover_select = lambda px, api, info: api == INIT_PRODUCER_ID and px.hold_time is not None
        proxy.takeover_release_inside = kind == "timeout_takeover_inside"
    elif kind == "takeover":
        proxy.takeover_select = lambda px, api, info: api == ADD_PARTITIONS_TO_TXN and px.add_partitions_from_a() == takeover_at_add
    proxy.start()
    try:
        a = _spawn(run, f"writer-a-{token}", "A", ports["A"], batches_file, workdir)
        b: Writer | None = None
        deadline = time.monotonic() + PROCESS_DEADLINE_S
        while time.monotonic() < deadline:
            _poll(a)
            if proxy.takeover_select is not None and b is None:
                fallback = (kind.startswith("timeout_takeover") and proxy.hold_time is not None
                            and proxy.now() > proxy.hold_time + DELIVERY_TIMEOUT_S + 4)
                if proxy.takeover_needed.is_set() or fallback or a.code is not None:
                    b = _spawn(run, f"writer-b-{token}", "B", ports["B"], batches_file, workdir)
            if b is not None:
                _poll(b)
            if a.code is not None and (proxy.takeover_select is None or (b is not None and b.code is not None)):
                break
            time.sleep(0.05)
        for w in run.writers.values():
            if not _poll(w):
                w.proc.kill()
                w.proc.wait(5)
                w.code, w.ended = "killed", time.monotonic()
        # let a fallback replay land before reading, so the ledger shows its effect
        if proxy.hold_time is not None and proxy.replay_result is None:
            wait_until = time.monotonic() + max(0.0, proxy.hold_time + proxy.release_fallback_s + 3 - proxy.now())
            while proxy.replay_result is None and time.monotonic() < wait_until:
                time.sleep(0.2)
    finally:
        run.events = proxy.snapshot()
        run.violations = list(proxy.violations)
        run.hold_time = proxy.hold_time
        run.replay = proxy.replay_result
        run.takeover_phase = proxy.takeover_phase
        proxy.stop()
    run.lso, run.hwm = settle_offsets(bootstrap, run.topic)
    run.committed = read_committed(bootstrap, run.topic)
    return run
