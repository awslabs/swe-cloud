"""Verifier harness: AWS fixtures, a GC Notify stub with a fault plan, and a worker supervisor.

Nothing here imports the solver's code. Workers and the callback server are started as
separate processes (`python3 -m reliability ...`) with PYTHONPATH pointing at the app tree, each
in its own session and its own empty working directory, HOME and TMPDIR.
"""
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
import collections
import datetime
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3

APP_DIR = os.environ.get("RELIABILITY_APP_DIR", "/app")
LOG_DIR = os.environ.get("VERIFIER_LOG_DIR", "/logs/verifier")

NOTIFY_PORT = 8025
CALLBACK_PORT = 8026
WORKER_COUNT = 4
RESPAWN_DELAY_S = 1.0
VISIBILITY_TIMEOUT_S = 12
MAX_RECEIVE_COUNT = 5
WARMUP_S = 4.0
PHASE2_OFFSET_S = 5.0
SLOW_SEND_HOLD_S = 15.0
SLOW_SEND_EXTRA_COPY_AFTER_S = 12.0
BARRIER_COUNT = 2
BARRIER_TIMEOUT_S = 2.0
CALLBACK_DELAY_S = 1.0
STALE_CALLBACK_DELAY_S = 1.0
CALLBACK_RETRY_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)  # 6 retries, production's 25 min compressed
CALLBACK_RESPONSE_LIMIT_S = 1.0
DRAIN_DEADLINE_S = 90.0
SETTLE_S = 14.0
NOTIFY_RETENTION_S = 7 * 86400              # docs/notify-api.md "Limits"
RATE_LIMIT_REQUESTS = 1000                  # per RATE_LIMIT_WINDOW_S, per API key, every call
RATE_LIMIT_WINDOW_S = 60.0
HISTORY_OLD = 1500                          # done long ago: Notify no longer has their emails
HISTORY_RECENT = 150                        # done in the last few days: Notify lists them, delivered


def now_iso(ts=None):
    moment = datetime.datetime.fromtimestamp(time.time() if ts is None else ts, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ----------------------------------------------------------------------------- process helpers
def _listening_inodes(port):
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as handle:
                rows = handle.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            parts = row.split()
            if len(parts) < 10:
                continue
            if int(parts[1].rsplit(":", 1)[1], 16) == port and parts[3] == "0A":
                inodes.add(parts[9])
    return inodes


def _connection_inodes(local_port, remote_port):
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as handle:
                rows = handle.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            parts = row.split()
            if len(parts) < 10:
                continue
            lport = int(parts[1].rsplit(":", 1)[1], 16)
            rport = int(parts[2].rsplit(":", 1)[1], 16)
            if lport == local_port and rport == remote_port and parts[9] != "0":
                inodes.add(parts[9])
    return inodes


def _pids_holding(inodes):
    wanted = {f"socket:[{inode}]" for inode in inodes}
    found = []
    if not wanted:
        return found
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{name}/fd"):
                try:
                    if os.readlink(f"/proc/{name}/fd/{fd}") in wanted:
                        found.append(int(name))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return found


def free_port(port):
    """Kill whatever is listening on `port` (left-over dev processes)."""
    me = os.getpid()
    for pid in _pids_holding(_listening_inodes(port)):
        if pid != me:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def kill_strays():
    subprocess.run(["pkill", "-9", "-f", "[-]m reliability"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "-f", "[f]ake_notify"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for port in (NOTIFY_PORT, CALLBACK_PORT):
        free_port(port)
    time.sleep(0.5)


def wait_port(port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


# ----------------------------------------------------------------------------- supervisor
class Slot:
    def __init__(self, index):
        self.index = index
        self.incarnation = 0
        self.proc = None
        self.worker_id = None
        self.workdir = None
        self.log = None
        self.died_at = None


class Supervisor:
    """Keeps WORKER_COUNT worker processes and one callback server running; respawns the dead."""

    def __init__(self, env, log_dir):
        self.env = env
        self.log_dir = log_dir
        self.slots = [Slot(i + 1) for i in range(WORKER_COUNT)]
        self.callback = Slot(0)
        self.lock = threading.RLock()
        self.stopping = False
        self.events = []
        self.thread = threading.Thread(target=self._monitor, daemon=True)

    def _event(self, **fields):
        fields["t"] = round(time.time(), 3)
        with self.lock:
            self.events.append(fields)

    def _spawn(self, slot, argv, label):
        slot.incarnation += 1
        if slot.workdir:
            shutil.rmtree(slot.workdir, ignore_errors=True)
        slot.workdir = tempfile.mkdtemp(prefix=f"rq-{label}-")
        env = dict(self.env)
        env.update({"HOME": slot.workdir, "TMPDIR": slot.workdir, "PYTHONPATH": APP_DIR,
                    "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        if slot.log:
            slot.log.close()
        slot.log = open(os.path.join(self.log_dir, f"{label}.log"), "ab")
        slot.log.write(f"\n===== start {label} incarnation {slot.incarnation} {now_iso()}\n".encode())
        slot.log.flush()
        slot.proc = subprocess.Popen(argv, cwd=slot.workdir, env=env, stdout=slot.log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        slot.died_at = None
        self._event(kind="spawn", label=label, pid=slot.proc.pid, incarnation=slot.incarnation)

    def _spawn_worker(self, slot):
        slot.worker_id = f"w{slot.index}-{slot.incarnation + 1}-{uuid.uuid4().hex[:6]}"
        self._spawn(slot, [sys.executable, "-m", "reliability", "worker", "--worker-id",
                           slot.worker_id], f"worker{slot.index}")

    def _spawn_callback(self):
        self._spawn(self.callback, [sys.executable, "-m", "reliability", "callback", "--port",
                                    str(CALLBACK_PORT)], "callback")

    def start_callback(self):
        with self.lock:
            self._spawn_callback()

    def start_workers(self):
        with self.lock:
            for slot in self.slots:
                self._spawn_worker(slot)
        self.thread.start()

    def _monitor(self):
        while not self.stopping:
            with self.lock:
                for slot in self.slots + [self.callback]:
                    if slot.proc is None or self.stopping:
                        continue
                    if slot.proc.poll() is not None:
                        if slot.died_at is None:
                            slot.died_at = time.time()
                            self._event(kind="exit", pid=slot.proc.pid, code=slot.proc.returncode,
                                        label="callback" if slot is self.callback else slot.worker_id)
                        elif time.time() - slot.died_at >= RESPAWN_DELAY_S:
                            if slot is self.callback:
                                self._spawn_callback()
                            else:
                                self._spawn_worker(slot)
            time.sleep(0.1)

    def slot_for_pid(self, pid):
        try:
            sid = os.getsid(pid)
        except OSError:
            return None
        with self.lock:
            for slot in self.slots + [self.callback]:
                if slot.proc is not None and slot.proc.poll() is None and slot.proc.pid == sid:
                    return slot
        return None

    def slot_for_worker_id(self, worker_id):
        with self.lock:
            for slot in self.slots:
                if slot.worker_id == worker_id and slot.proc is not None and slot.proc.poll() is None:
                    return slot
        return None

    def kill(self, slot, reason):
        with self.lock:
            proc = slot.proc
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            slot.died_at = time.time()
            self._event(kind="kill", pid=proc.pid, reason=reason,
                        worker="callback" if slot is self.callback else slot.worker_id)

    def stop(self):
        self.stopping = True
        with self.lock:
            for slot in self.slots + [self.callback]:
                if slot.proc is not None and slot.proc.poll() is None:
                    try:
                        os.killpg(slot.proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        slot.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                if slot.log:
                    slot.log.close()
                    slot.log = None
                if slot.workdir:
                    shutil.rmtree(slot.workdir, ignore_errors=True)


# ----------------------------------------------------------------------------- notify stub
RATE_LIMITED = {"status_code": 429, "errors": [{
    "error": "RateLimitError",
    "message": f"Exceeded rate limit for key type LIVE of {RATE_LIMIT_REQUESTS} requests per "
               f"{int(RATE_LIMIT_WINDOW_S)} seconds"}]}


class NotifyStub:
    """GC Notify double. Records every accepted send; applies a plan keyed by recipient address.

    plan[address] = {"name": ..., "kind": one of
        "normal" | "barrier" | "accept_then_kill" | "accept_bounce_then_kill" | "drop_then_kill" |
        "slow" | "flaky" | "bounce"}
    """

    def __init__(self, supervisor, plan, callback_url, callback_token, hooks):
        self.supervisor = supervisor
        self.plan = plan
        self.by_name = {entry["name"]: address for address, entry in plan.items()}
        self.callback_url = callback_url
        self.callback_token = callback_token
        self.hooks = hooks
        self.lock = threading.Condition()
        self.notifications = []      # every recorded (accepted) send, in order
        self.requests = []           # every request seen, for evidence
        self.send_requests = {}      # address -> number of send requests received
        self.callbacks = []          # every callback attempt
        self.violations = []
        self.active = 0
        self.pending_callbacks = 0
        self.barrier_waiting = 0
        self.barrier_generation = 0
        self.history_addresses = set()
        self.served = collections.deque()  # times of the requests inside the rate-limit window
        self.rate_limited = {"send": 0, "get": 0}
        self.server = None

    # -- bookkeeping ----------------------------------------------------------------------
    def busy(self):
        with self.lock:
            return self.active > 0 or self.pending_callbacks > 0

    def sends_for(self, name):
        address = self.by_name[name]
        with self.lock:
            return [dict(n) for n in self.notifications if n["email_address"] == address]

    def snapshot(self):
        with self.lock:
            return {
                "notifications": [dict(n) for n in self.notifications],
                "requests": list(self.requests),
                "callbacks": list(self.callbacks),
                "violations": list(self.violations),
            }

    def _record(self, body, handler):
        notification = {
            "id": str(uuid.uuid4()),
            "reference": body.get("reference"),
            "email_address": body.get("email_address"),
            "template_id": body.get("template_id"),
            "type": "email",
            "status": "created",
            "created_at": now_iso(),
            "t": round(time.time(), 3),
            "worker_header": handler.headers.get("X-Worker-Id"),
        }
        with self.lock:
            self.notifications.append(notification)
        return notification

    def admit(self, op):
        """Notify's per-key rate limit: a request over it is answered 429 and not processed."""
        now = time.time()
        with self.lock:
            while self.served and self.served[0] <= now - RATE_LIMIT_WINDOW_S:
                self.served.popleft()
            if len(self.served) >= RATE_LIMIT_REQUESTS:
                self.rate_limited[op] += 1
                return False
            self.served.append(now)
            return True

    def seed(self, address, reference, status, age_s):
        """A notification the previous version of the service had sent before the deploy."""
        created = time.time() - age_s
        notification = {
            "id": str(uuid.uuid4()),
            "reference": reference,
            "email_address": address,
            "template_id": None,
            "type": "email",
            "status": status,
            "created_at": now_iso(created),
            "t": round(created, 3),
            "worker_header": None,
            "before_deploy": True,
        }
        with self.lock:
            self.notifications.append(notification)
        return notification

    def _barrier(self):
        with self.lock:
            generation = self.barrier_generation
            self.barrier_waiting += 1
            if self.barrier_waiting >= BARRIER_COUNT:
                self.barrier_waiting = 0
                self.barrier_generation += 1
                self.lock.notify_all()
                return
            deadline = time.time() + BARRIER_TIMEOUT_S
            while self.barrier_generation == generation:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self.barrier_waiting -= 1
                    return
                self.lock.wait(remaining)

    # -- callbacks ------------------------------------------------------------------------
    def schedule_callback(self, notification, status, delay, on_first_ack=None):
        with self.lock:
            self.pending_callbacks += 1
        threading.Thread(target=self._callback_thread,
                         args=(notification, status, delay, on_first_ack), daemon=True).start()

    def _callback_thread(self, notification, status, delay, on_first_ack):
        try:
            time.sleep(delay)
            with self.lock:
                notification_live = next(n for n in self.notifications if n["id"] == notification["id"])
                notification_live["status"] = status
            payload = {
                "id": notification["id"],
                "reference": notification["reference"],
                "to": notification["email_address"],
                "status": status,
                "status_description": {"delivered": "Delivered",
                                       "permanent-failure": "No such address",
                                       "temporary-failure": "Content or inbox issue"}.get(status, "Tech issue"),
                "provider_response": None,
                "created_at": notification["created_at"],
                "completed_at": now_iso(),
                "sent_at": notification["created_at"],
                "notification_type": "email",
            }
            data = json.dumps(payload).encode()
            for attempt in range(len(CALLBACK_RETRY_BACKOFF_S) + 1):
                started = time.time()
                code = None
                try:
                    request = urllib.request.Request(self.callback_url, data=data, method="POST")
                    request.add_header("Content-Type", "application/json")
                    request.add_header("Authorization", f"Bearer {self.callback_token}")
                    with urllib.request.urlopen(request, timeout=CALLBACK_RESPONSE_LIMIT_S) as response:
                        code = response.status
                        response.read()
                except urllib.error.HTTPError as error:
                    code = error.code
                except Exception as error:  # timeout, refused
                    code = f"error:{type(error).__name__}"
                elapsed = time.time() - started
                ok = isinstance(code, int) and 200 <= code < 300 and elapsed <= CALLBACK_RESPONSE_LIMIT_S
                with self.lock:
                    self.callbacks.append({"t": round(started, 3), "id": notification["id"],
                                           "reference": notification["reference"], "status": status,
                                           "attempt": attempt, "code": code, "elapsed": round(elapsed, 3)})
                if ok:
                    if on_first_ack:
                        on_first_ack()
                    return
                if attempt < len(CALLBACK_RETRY_BACKOFF_S):
                    time.sleep(CALLBACK_RETRY_BACKOFF_S[attempt])
        finally:
            with self.lock:
                self.pending_callbacks -= 1

    def _after_send(self, entry, notification):
        """Delivery receipts for an accepted send, per plan."""
        kind = entry["kind"]
        with self.lock:
            accepted = [n for n in self.notifications if n["email_address"] == notification["email_address"]]
        index = next(i for i, n in enumerate(accepted) if n["id"] == notification["id"]) + 1
        if kind == "flaky" and index == 1:
            def first_ack():
                # The same receipt again straight away, and another copy of the submission
                # arriving on the work queue.
                self.schedule_callback(notification, "temporary-failure", 0.0)
                self.hooks["inject"](entry["name"])
            self.schedule_callback(notification, "temporary-failure", CALLBACK_DELAY_S, first_ack)
            return
        if kind == "flaky" and index == 2:
            # The first notification's failure receipt arrives once more, late.
            self.schedule_callback(accepted[0], "temporary-failure", STALE_CALLBACK_DELAY_S)
            self.schedule_callback(notification, "delivered", CALLBACK_DELAY_S)
            return
        if kind == "accept_bounce_then_kill" and index == 1:
            # Full inbox: the bounce comes back while nobody has recorded this email yet.
            self.schedule_callback(notification, "temporary-failure", CALLBACK_DELAY_S)
            return
        if kind == "bounce":
            self.schedule_callback(notification, "permanent-failure", CALLBACK_DELAY_S)
            return
        self.schedule_callback(notification, "delivered", CALLBACK_DELAY_S)

    # -- kill attribution -----------------------------------------------------------------
    def _kill_requester(self, handler, reason):
        client_port = handler.client_address[1]
        slot = None
        for _ in range(5):
            for pid in _pids_holding(_connection_inodes(client_port, NOTIFY_PORT)):
                slot = self.supervisor.slot_for_pid(pid)
                if slot is not None:
                    break
            if slot is not None:
                break
            time.sleep(0.05)
        if slot is None:
            header = handler.headers.get("X-Worker-Id")
            if header:
                slot = self.supervisor.slot_for_worker_id(header)
        if slot is None:
            with self.lock:
                self.violations.append(f"could not attribute a send to a supervised worker ({reason})")
            return False
        self.supervisor.kill(slot, reason)
        return True

    # -- HTTP -----------------------------------------------------------------------------
    def make_handler(stub):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    self.wfile.flush()
                except OSError:
                    self.close_connection = True

            def do_POST(self):
                with stub.lock:
                    stub.active += 1
                try:
                    self._post()
                finally:
                    with stub.lock:
                        stub.active -= 1

            def do_GET(self):
                with stub.lock:
                    stub.active += 1
                try:
                    self._get()
                finally:
                    with stub.lock:
                        stub.active -= 1

            def _post(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if urllib.parse.urlparse(self.path).path != "/v2/notifications/email":
                    return self._reply(404, {"status_code": 404, "errors": [{"error": "NotFound"}]})
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._reply(400, {"status_code": 400, "errors": [{"error": "ValidationError"}]})
                if not stub.admit("send"):
                    return self._reply(429, RATE_LIMITED)
                if not body.get("email_address") or not body.get("template_id"):
                    return self._reply(400, {"status_code": 400, "errors": [{"error": "ValidationError",
                                             "message": "email_address and template_id are required"}]})
                address = body["email_address"]
                entry = stub.plan.get(address, {"name": None, "kind": "normal"})
                with stub.lock:
                    nth = stub.send_requests.get(address, 0) + 1
                    stub.send_requests[address] = nth
                    stub.requests.append({"t": round(time.time(), 3), "op": "send", "name": entry["name"],
                                          "nth": nth, "reference": body.get("reference"),
                                          "worker_header": self.headers.get("X-Worker-Id")})
                kind = entry["kind"]
                if (kind == "drop_then_kill" and nth == 1) or (kind == "flaky" and nth == 2):
                    stub._kill_requester(self, f"{entry['name']} send #{nth} dropped")
                    self.close_connection = True
                    return
                notification = stub._record(body, self)
                if kind in ("accept_then_kill", "accept_bounce_then_kill") and nth == 1:
                    stub._kill_requester(self, f"{entry['name']} send #1 accepted")
                    self.close_connection = True
                    stub._after_send(entry, notification)
                    return
                if kind == "barrier":
                    stub._barrier()
                if kind == "slow" and nth == 1:
                    time.sleep(SLOW_SEND_HOLD_S)
                self._reply(201, {"id": notification["id"], "reference": notification["reference"],
                                  "content": {"subject": (body.get("personalisation") or {}).get("subject")},
                                  "template": {"id": body.get("template_id"), "version": 1}})
                stub._after_send(entry, notification)

            def _get(self):
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                reference = query.get("reference", [None])[0]
                if not stub.admit("get"):
                    return self._reply(429, RATE_LIMITED)
                with stub.lock:
                    if len(stub.requests) < 20000:
                        stub.requests.append({"t": round(time.time(), 3), "op": "get", "path": self.path,
                                              "worker_header": self.headers.get("X-Worker-Id")})
                if reference is not None:
                    for address, entry in stub.plan.items():
                        if entry["kind"] == "barrier" and entry["sid"] in reference:
                            stub._barrier()
                            break
                retained_since = time.time() - NOTIFY_RETENTION_S
                with stub.lock:
                    items = [{k: n[k] for k in ("id", "reference", "email_address", "type", "status",
                                                "created_at")}
                             for n in reversed(stub.notifications) if n["t"] >= retained_since]
                if parsed.path == "/v2/notifications":
                    if reference is not None:
                        items = [n for n in items if n["reference"] == reference]
                    if "status" in query:
                        items = [n for n in items if n["status"] == query["status"][0]]
                    return self._reply(200, {"notifications": items[:250], "links": {}})
                if parsed.path.startswith("/v2/notifications/"):
                    wanted = parsed.path.rsplit("/", 1)[1]
                    for n in items:
                        if n["id"] == wanted:
                            return self._reply(200, n)
                    return self._reply(404, {"status_code": 404, "errors": [{"error": "NoResultFound",
                                                                             "message": "No result found"}]})
                return self._reply(404, {"status_code": 404, "errors": [{"error": "NotFound"}]})

        return Handler

    def start(self):
        ThreadingHTTPServer.allow_reuse_address = True
        self.server = ThreadingHTTPServer(("127.0.0.1", NOTIFY_PORT), self.make_handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# ----------------------------------------------------------------------------- the run
SUBMISSIONS = [
    # name, plan kind, phase
    ("B1", "normal", 2),
    ("S1", "accept_then_kill", 2),
    ("S2", "drop_then_kill", 2),
    ("S3", "flaky", 2),
    ("S4", "barrier", 1),
    ("S5", "slow", 2),
    ("S6", "bounce", 2),
    ("S7", "accept_bounce_then_kill", 2),
    # left behind by the previous version at the deploy (phase 0, see LEGACY)
    ("L1", "normal", 0),
    ("L2", "normal", 0),
    ("L3", "normal", 0),
    ("L4", "normal", 0),
    ("L5", "normal", 0),
    ("L6", "normal", 0),
    ("L7", "normal", 0),
    ("L8", "normal", 0),
]

# What the current code (the starter: reference = SubmissionID, unconditional SENT after the
# send, callback resets any non-final status to PENDING and re-queues) leaves in Notify, the
# table and the queues when its instances are stopped for the deploy. emails: statuses of the
# notifications it sent, oldest first; nid: index of the one the item records; queue: pointers
# still on the work queues; receipts: (email index, status, seconds after t0) Notify has yet to
# deliver to the callback server.
LEGACY = {
    # a worker died after Notify accepted its send, before recording it
    "L1": {"emails": ["sending"], "state": "PENDING", "nid": None, "queue": ["submission"],
           "receipts": [(0, "delivered", 1.0)]},
    # a bounce reset the item; the receipt came twice, so there are two reprocess pointers
    "L2": {"emails": ["temporary-failure"], "state": "PENDING", "nid": 0,
           "queue": ["reprocess", "reprocess"], "receipts": [(0, "temporary-failure", 3.0)]},
    # the replacement went out and was delivered; a late repeat of the first bounce reset the
    # item again (the "three copies" owner, one deploy earlier)
    "L3": {"emails": ["temporary-failure", "delivered"], "state": "PENDING", "nid": 1,
           "queue": ["reprocess"], "receipts": []},
    # a worker sent the replacement and died before recording it
    "L4": {"emails": ["temporary-failure", "sending"], "state": "PENDING", "nid": 0,
           "queue": ["reprocess"], "receipts": [(1, "delivered", 1.0)]},
    # sent and recorded; Notify reports a bounce after the deploy
    "L5": {"emails": ["sending"], "state": "SENT", "nid": 0, "queue": [],
           "receipts": [(0, "temporary-failure", 2.0)]},
    # never sent: Notify was erroring for a while and the pointer went to the DLQ
    "L6": {"emails": [], "state": "PENDING", "nid": None, "queue": ["dlq"], "receipts": []},
    # a bounce reset the item and an old worker was sending the replacement when the deploy
    # stopped it: it holds one reprocess pointer (back after the visibility timeout), the
    # repeated receipt left a second one, and Notify accepts the replacement request
    # LEGACY_LATE_ACCEPT_S after the new instances start (inside the documented 2 s)
    "L7": {"emails": ["temporary-failure"], "state": "PENDING", "nid": 0,
           "queue": ["reprocess", "submission", "held:reprocess"], "receipts": [(1, "delivered", 2.0)],
           "late_accept": True},
    # a bounce reset the item and its one receipt was acknowledged; no worker took the pointer yet
    "L8": {"emails": ["temporary-failure"], "state": "PENDING", "nid": 0, "queue": ["reprocess"],
           "receipts": []},
}
LEGACY_LATE_ACCEPT_S = 1.7
LEGACY_EMAIL_AGE_S = (40.0, 20.0)


def aws_ready():
    try:
        boto3.client("sqs").list_queues()
        boto3.client("dynamodb").list_tables()
        return True
    except Exception:
        return False


def form_item(sid, address):
    form_data = {
        "formID": "clf9q2b7e0001mk08ab3hd2xq",
        "language": "en",
        "securityAttribute": "Protected A",
        "responses": {"1": f"Applicant {sid[:8]}", "2": "Request for records", "3": "No"},
        "deliveryOption": {"emailAddress": address, "emailSubjectEn": "Access to information request"},
        "form": {"titleEn": "Access to information", "titleFr": "Accès à l'information"},
    }
    return {
        "SubmissionID": {"S": sid},
        "FormID": {"S": form_data["formID"]},
        "SendReceipt": {"S": "unknown"},
        "FormSubmissionLanguage": {"S": "en"},
        "FormData": {"S": json.dumps(form_data)},
        "CreatedAt": {"N": str(int(time.time() * 1000))},
        "SecurityAttribute": {"S": "Protected A"},
        "NotifyState": {"S": "PENDING"},
    }


def seed_legacy(stub, ddb, sqs, table, subs, queues, injections):
    """Put the previous version's leftovers in place before the new instances start."""
    seeded = {}
    # The in-flight one first, so its visible pointers are near the head of the queues.
    for name, spec in sorted(LEGACY.items(), key=lambda kv: not kv[1].get("late_accept")):
        info = subs[name]
        emails = [stub.seed(info["address"], info["sid"], status, LEGACY_EMAIL_AGE_S[i])
                  for i, status in enumerate(spec["emails"])]
        item = form_item(info["sid"], info["address"])
        item["CreatedAt"] = {"N": str(int((time.time() - 45) * 1000))}
        item["NotifyState"] = {"S": spec["state"]}
        if spec["nid"] is not None:
            item["NotificationId"] = {"S": emails[spec["nid"]]["id"]}
            item["SentAt"] = {"N": str(int(emails[spec["nid"]]["t"]))}
        ddb.put_item(TableName=table, Item=item)
        for role in spec["queue"]:
            held = role.startswith("held:")
            role = role.split(":")[-1]
            sqs.send_message(QueueUrl=queues[role], MessageBody=json.dumps({"submissionID": info["sid"]}))
            if held:
                # Received by the stopped worker: invisible until its visibility timeout ends.
                for _ in range(10):
                    if sqs.receive_message(QueueUrl=queues[role], MaxNumberOfMessages=1,
                                           WaitTimeSeconds=1).get("Messages"):
                        break
            injections.append({"t": round(time.time(), 3), "name": name, "queue": role, "held": held,
                               "before_deploy": True})
        seeded[name] = emails
    return seeded


def seed_history(stub, ddb, table):
    """Submissions the service finished before this deploy, the way the current code leaves them.

    Items are never deleted (docs/OPERATIONS.md), so the table holds every response since launch.
    The old ones are past Notify's 7-day retention: Notify neither lists their emails by reference
    nor answers for their ids. The recent ones are still listed, delivered.
    """
    history = {}
    rows = []
    rng = random.Random()
    for index in range(HISTORY_OLD + HISTORY_RECENT):
        recent = index >= HISTORY_OLD
        sid = str(uuid.uuid4())
        address = f"owner-{uuid.uuid4().hex[:10]}@forms-test.gc.ca"
        age = rng.uniform(0.5, 6.5) * 86400 if recent else rng.uniform(8.0, 120.0) * 86400
        if recent:
            nid = stub.seed(address, sid, "delivered", age - 3)["id"]
        else:
            nid = str(uuid.uuid4())
        item = form_item(sid, address)
        item["CreatedAt"] = {"N": str(int((time.time() - age) * 1000))}
        item["SendReceipt"] = {"S": str(uuid.uuid4())}
        item["NotifyState"] = {"S": "SENT"}
        item["NotificationId"] = {"S": nid}
        item["SentAt"] = {"N": str(int(time.time() - age + 3))}
        history[sid] = {"address": address, "nid": nid, "recent": recent}
        rows.append(item)
    for start in range(0, len(rows), 25):
        requests = [{"PutRequest": {"Item": item}} for item in rows[start:start + 25]]
        for _ in range(20):
            try:
                unprocessed = ddb.batch_write_item(RequestItems={table: requests}).get("UnprocessedItems", {})
            except Exception:
                for request in requests:
                    ddb.put_item(TableName=table, Item=request["PutRequest"]["Item"])
                break
            requests = unprocessed.get(table, [])
            if not requests:
                break
            time.sleep(0.2)
    stub.history_addresses = {info["address"] for info in history.values()}
    return history


def _history_final(ddb, table, history, notifications):
    """Which finished submissions the new version emailed again or rewrote."""
    found = {}
    arguments = dict(TableName=table, ConsistentRead=True,
                     ProjectionExpression="SubmissionID, NotifyState, NotificationId")
    while True:
        page = ddb.scan(**arguments)
        for row in page.get("Items", []):
            sid = row["SubmissionID"]["S"]
            if sid in history:
                found[sid] = _plain(row)
        if not page.get("LastEvaluatedKey"):
            break
        arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    by_address = collections.Counter(n["email_address"] for n in notifications if not n.get("before_deploy"))
    resent, changed = [], []
    for sid, info in history.items():
        sends = by_address.get(info["address"], 0)
        if sends:
            resent.append({"sid": sid, "recent": info["recent"], "sends": sends})
        row = found.get(sid)
        if (row is None or row.get("NotifyState") not in ("SENT", "DELIVERED")
                or row.get("NotificationId") != info["nid"]):
            changed.append({"sid": sid, "recent": info["recent"], "seeded": info["nid"], "now": row})
    return {"count": len(history), "old": HISTORY_OLD, "recent": HISTORY_RECENT,
            "resent_count": len(resent), "resent": resent[:20],
            "changed_count": len(changed), "changed": changed[:20]}


def _release(stub):
    with stub.lock:
        stub.pending_callbacks -= 1


def _plain(item):
    out = {}
    for name, value in (item or {}).items():
        (kind, raw), = value.items()
        out[name] = raw
    return out


def _seed_resources(sqs, ddb, suffix):
    table = f"ReliabilityQueue-{suffix}"
    outputs = deploy(boto3.client("cloudformation"), "notification-" + suffix,
                     Path(__file__).parent / "cdk_seed/seed.template.json",
                     DlqName=f"submission-dlq-{suffix}", SubmissionName=f"submission-queue-{suffix}",
                     ReprocessName=f"reprocess-queue-{suffix}", TableName=table)
    return outputs["DlqUrl"], outputs["SubmissionUrl"], outputs["ReprocessUrl"], table


def run_scenario():
    os.makedirs(LOG_DIR, exist_ok=True)
    result = {"ok": False, "error": None, "notes": []}
    if not aws_ready():
        with open(os.path.join(LOG_DIR, "environment"), "w") as handle:
            handle.write("AWS endpoint not answering\n")
        result["error"] = "environment: AWS endpoint not answering"
        return result

    kill_strays()
    sqs = boto3.client("sqs")
    ddb = boto3.client("dynamodb")
    suffix = uuid.uuid4().hex[:8]
    queues_before = {url.rstrip("/").rsplit("/", 1)[-1] for url in sqs.list_queues().get("QueueUrls", [])}

    dlq_url, submission_url, reprocess_url, table = _seed_resources(sqs, ddb, suffix)
    for _ in range(60):
        status = ddb.describe_table(TableName=table)["Table"].get("TableStatus")
        if status == "ACTIVE":
            break
        time.sleep(0.5)

    subs = {}
    plan = {}
    for name, kind, phase in SUBMISSIONS:
        sid = str(uuid.uuid4())
        address = f"owner-{uuid.uuid4().hex[:10]}@forms-test.gc.ca"
        subs[name] = {"sid": sid, "address": address, "phase": phase}
        plan[address] = {"name": name, "kind": kind, "sid": sid}
        ddb.put_item(TableName=table, Item=form_item(sid, address))

    token = uuid.uuid4().hex
    env = dict(os.environ)
    env.update({
        "SUBMISSION_QUEUE_URL": submission_url,
        "REPROCESS_QUEUE_URL": reprocess_url,
        "DLQ_URL": dlq_url,
        "TABLE_NAME": table,
        "NOTIFY_API_URL": f"http://127.0.0.1:{NOTIFY_PORT}",
        "NOTIFY_API_KEY": "gcforms-verifier-" + suffix,
        "NOTIFY_TEMPLATE_ID": str(uuid.uuid4()),
        "NOTIFY_CALLBACK_TOKEN": token,
        "CALLBACK_PORT": str(CALLBACK_PORT),
        "VISIBILITY_TIMEOUT_S": str(VISIBILITY_TIMEOUT_S),
    })
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")

    injections = []
    inj_lock = threading.Lock()

    inject_client = boto3.session.Session().client("sqs")

    def inject(name):
        with inj_lock:
            inject_client.send_message(QueueUrl=submission_url,
                                       MessageBody=json.dumps({"submissionID": subs[name]["sid"]}))
            injections.append({"t": round(time.time(), 3), "name": name})

    pending_timers = []
    history = {}
    supervisor = Supervisor(env, LOG_DIR)
    stub = NotifyStub(supervisor, plan, f"http://127.0.0.1:{CALLBACK_PORT}/api/notify-callback",
                      token, {"inject": inject})
    result.update({"subs": subs, "queues": {"submission": submission_url, "reprocess": reprocess_url,
                                            "dlq": dlq_url}, "table": table,
                   "queues_before": sorted(queues_before)})
    try:
        stub.start()
        history = seed_history(stub, ddb, table)
        legacy = seed_legacy(stub, ddb, sqs, table, subs, result["queues"], injections)
        supervisor.start_callback()
        if not wait_port(CALLBACK_PORT, 30):
            result["notes"].append("callback server never listened on port %d" % CALLBACK_PORT)

        def late_accept(name):
            info = subs[name]
            notification = stub.seed(info["address"], info["sid"], "sending", 0.0)
            legacy[name].append(notification)
            for index, status, delay in LEGACY[name]["receipts"]:
                stub.schedule_callback(legacy[name][index], status, delay)

        for name, spec in LEGACY.items():
            if spec.get("late_accept"):
                timer = threading.Timer(LEGACY_LATE_ACCEPT_S, late_accept, args=(name,))
                pending_timers.append(timer)
                with stub.lock:
                    stub.pending_callbacks += 1   # keeps the run from converging before it fires
                threading.Thread(target=lambda t=timer: (t.start(), t.join(),
                                                         _release(stub)), daemon=True).start()
        result["deployed_at"] = time.time()
        supervisor.start_workers()
        time.sleep(WARMUP_S)

        t0 = time.time()
        result["t0"] = t0
        for name, spec in LEGACY.items():
            if spec.get("late_accept"):
                continue
            for index, status, delay in spec["receipts"]:
                stub.schedule_callback(legacy[name][index], status, delay)
        inject("S4")
        inject("S4")
        time.sleep(PHASE2_OFFSET_S)
        for name, _, phase in SUBMISSIONS:
            if phase == 2:
                inject(name)
        timer = threading.Timer(SLOW_SEND_EXTRA_COPY_AFTER_S, inject, args=("S5",))
        timer.start()
        pending_timers.append(timer)

        deadline = t0 + PHASE2_OFFSET_S + DRAIN_DEADLINE_S
        settled_since = None
        settled_log = None
        why = [None]
        while time.time() < deadline:
            if timer.is_alive():
                why[0] = "extra copy not injected yet"
                done = False
            elif stub.busy():
                why[0] = "notify stub busy (request in flight or callback pending)"
                done = False
            else:
                done = _converged(ddb, sqs, table, subs, stub, result["queues"], why)
            if done:
                log_now = len(stub.snapshot()["notifications"])
                if settled_since is None or settled_log != log_now:
                    settled_since, settled_log = time.time(), log_now
                elif time.time() - settled_since >= SETTLE_S:
                    result["converged_at"] = round(time.time() - t0, 1)
                    break
            else:
                settled_since = None
            time.sleep(0.5)
        else:
            result["notes"].append(f"drain deadline reached; last reason: {why[0]}")
        result["ok"] = result["error"] is None
    except Exception as error:  # surface harness failures as data, never as a missing score
        result["error"] = f"harness: {error!r}"
    finally:
        for timer in pending_timers:
            timer.cancel()
        supervisor.stop()
        time.sleep(0.5)
        try:
            result["final"] = _final(ddb, sqs, table, subs, result["queues"])
        except Exception as error:
            result["error"] = (result["error"] or "") + f" final-read: {error!r}"
            result["final"] = None
        stub.stop()
        snap = stub.snapshot()
        try:
            result["history"] = _history_final(ddb, table, history, snap["notifications"])
        except Exception as error:
            result["error"] = (result["error"] or "") + f" history-read: {error!r}"
            result["history"] = None
        with stub.lock:
            result["notify_rate_limited"] = dict(stub.rate_limited)
        snap["notifications"] = [n for n in snap["notifications"]
                                 if n["email_address"] not in stub.history_addresses or not n.get("before_deploy")]
        result["stub"] = snap
        result["sends"] = {name: [n for n in snap["notifications"] if n["email_address"] == subs[name]["address"]]
                           for name in subs}
        result["supervisor_events"] = supervisor.events
        result["injections"] = injections
        try:
            result["queues_after"] = sorted(url.rstrip("/").rsplit("/", 1)[-1]
                                            for url in sqs.list_queues().get("QueueUrls", []))
        except Exception:
            result["queues_after"] = None
        with open(os.path.join(LOG_DIR, "run.json"), "w") as handle:
            json.dump(result, handle, indent=2, default=str)
    return result


def _queue_counts(sqs, url):
    return sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]


def _converged(ddb, sqs, table, subs, stub, queues, why):
    """True when every item is done and matches Notify's log and every queue is empty."""
    try:
        for name, info in subs.items():
            item = _plain(ddb.get_item(TableName=table, Key={"SubmissionID": {"S": info["sid"]}},
                                       ConsistentRead=True).get("Item"))
            if item.get("NotifyState") not in ("SENT", "DELIVERED", "FAILED"):
                why[0] = f"{name} NotifyState={item.get('NotifyState')}"
                return False
            sends = stub.sends_for(name)
            if not sends or item.get("NotificationId") != sends[-1]["id"]:
                why[0] = f"{name} NotificationId does not match the last send"
                return False
        for role, url in queues.items():
            counts = _queue_counts(sqs, url)
            for key in ("ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
                        "ApproximateNumberOfMessagesDelayed"):
                if int(counts.get(key, "0")):
                    why[0] = f"{role} {key}={counts.get(key)}"
                    return False
        return True
    except Exception as error:
        why[0] = f"read failed: {error!r}"
        return False


def _final(ddb, sqs, table, subs, queues):
    items = {}
    for name, info in subs.items():
        items[name] = _plain(ddb.get_item(TableName=table, Key={"SubmissionID": {"S": info["sid"]}},
                                          ConsistentRead=True).get("Item"))
    counts = {role: _queue_counts(sqs, url) for role, url in queues.items()}
    return {"items": items, "queues": counts}
