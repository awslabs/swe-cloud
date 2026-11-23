"""Verifier scenario: run the deployed relay against its own RDS instance and FIFO queue under a
scheduled workload, kill it on a schedule, and record what Postgres and the queue show.

Nothing here imports /app; the relay is only ever run as `python3 /app/relay.py`.
T0 is the relay's first READY. All times below are seconds after T0.
"""
from __future__ import annotations

import json
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
import os
import random
import secrets
import signal
import string
import subprocess
import tempfile
import threading
import time
import traceback
import uuid

import boto3
import botocore.config
import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("VERIFIER_LOG_DIR", "/logs/verifier")

PRESEED_ROWS = 10000
INITIAL_CHECKPOINT = 10000
BG_WRITERS = 4
BG_TXN_MS = (60, 80)
BG_GAP_MAX_MS = 5
BG_STAGGER_MS = 20
BG_STOP_AT = 100.0
BYSTANDER_OPEN_AT = 2.0
BYSTANDER_LOCK_KEY = 51080001
ROLLBACK_DRAW_AT, ROLLBACK_AT = 5.0, 8.0
BURN_AT = 8.5
KILL_TIMES = (9.0, 13.0, 17.0, 50.0)
RESTART_DELAY = 1.0
FIRST_READY_TIMEOUT = 45.0
PHASE1_AT = 28.0           # 20 s after the rollback at 8 s
PHASE1_COMMITTED_BEFORE = 23.0
PHASE1_GRACE = 1.5         # receive / checkpoint-write slack on top of PHASE1_AT
SLOW_DRAW_AT, SLOW_COMMIT_AT = 30.0, 75.0
PHASE2_AT = 60.0
PHASE2_LATE_AT = 74.0
PAIR_A_XID_AT, PAIR_B_XID_AT = 80.0, 80.5
PAIR_B_INSERT_AT, PAIR_A_INSERT_AT = 81.0, 81.5
PAIR_A_COMMIT_AT, PAIR_B_COMMIT_AT = 82.0, 97.0
# Second inverted pair after background writers stop: no newer xid completes while B2 is open.
PAIR2_TIMES = (101.0, 101.5, 102.0, 102.5, 103.0, 112.0)   # A xid, B xid, B insert, A insert, A commit, B commit
PAIR2_CHECK_AT = 110.0
FINAL_AT = 130.0
FINAL_CATCHUP = 10.0
MAX_COPIES = 1 + len(KILL_TIMES)
BG_MIN_ROWS = 1500


def _log(msg):
    print(f"[scenario {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class EnvironmentFailure(RuntimeError):
    """The emulator or engine did not come up: not a property of the relay."""


class Clock:
    def __init__(self):
        self.t0 = None

    def now(self):
        return time.monotonic() - self.t0

    def sleep_until(self, t):
        d = self.t0 + t - time.monotonic()
        if d > 0:
            time.sleep(d)


# ---------------------------------------------------------------- provisioning

def provision():
    sfx = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    master_pw = secrets.token_hex(12)
    pw = {"app_writer": secrets.token_hex(10), "outbox_relay": secrets.token_hex(10)}
    rds = boto3.client("rds", config=botocore.config.Config(read_timeout=600, retries={"max_attempts": 1}))
    ident = f"ledger-verify-{sfx}"
    try:
        rds.create_db_instance(DBInstanceIdentifier=ident, Engine="postgres", EngineVersion="16.3",
                               DBInstanceClass="db.t3.micro", AllocatedStorage=20, DBName="ledger",
                               MasterUsername="verifier_admin", MasterUserPassword=master_pw)
        deadline = time.monotonic() + 600
        while True:
            inst = rds.describe_db_instances(DBInstanceIdentifier=ident)["DBInstances"][0]
            if inst["DBInstanceStatus"] == "available" and inst.get("Endpoint"):
                break
            if time.monotonic() > deadline:
                raise EnvironmentFailure(f"RDS {ident} not available: {inst['DBInstanceStatus']}")
            time.sleep(1)
        host, port = inst["Endpoint"]["Address"], int(inst["Endpoint"]["Port"])
        admin = None
        deadline = time.monotonic() + 60
        while admin is None:
            try:
                admin = psycopg.connect(host=host, port=port, dbname="ledger", user="verifier_admin",
                                        password=master_pw, autocommit=True, connect_timeout=5)
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        admin.execute(open(os.path.join(HERE, "ddl_schema.sql")).read())
        admin.execute(open(os.path.join(HERE, "ddl_roles.sql")).read())
        for role, p in pw.items():
            admin.execute(f"alter role {role} password '{p}'")
        admin.execute(
            "insert into outbox (message_id, message_type, data) "
            "select 'hist-' || g, 'LedgerHistory', jsonb_build_object('n', g, 'seed', %s::text) "
            "from generate_series(1, %s) g", (sfx, PRESEED_ROWS))
        admin.execute("update relay_checkpoint set position = %s, updated_at = now() where id = 1", (INITIAL_CHECKPOINT,))
        assert admin.execute("select max(position) from outbox").fetchone()[0] == INITIAL_CHECKPOINT
        sqs = boto3.client("sqs")
        attrs = {"FifoQueue": "true", "ContentBasedDeduplication": "false", "DelaySeconds": "0"}
        url = deploy(boto3.client("cloudformation"), "queue-" + ident,
                     Path(__file__).parent / "cdk_seed/seed.template.json",
                     QueueName=f"ledger-events-{sfx}.fifo")["QueueUrl"]
    except EnvironmentFailure:
        raise
    except Exception as exc:
        raise EnvironmentFailure(f"provisioning failed: {exc!r}") from exc
    _log(f"provisioned {ident} at {host}:{port}, queue {url}")
    return {"host": host, "port": port, "master_pw": master_pw, "pw": pw, "queue_url": url, "sfx": sfx,
            "admin": admin}


def connect(ctx, user, autocommit, app="verifier"):
    pw = ctx["master_pw"] if user == "verifier_admin" else ctx["pw"][user]
    deadline = time.monotonic() + 20
    while True:
        try:
            return psycopg.connect(host=ctx["host"], port=ctx["port"], dbname="ledger", user=user, password=pw,
                                   autocommit=autocommit, connect_timeout=5,
                                   application_name=app)
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


# ---------------------------------------------------------------- relay supervisor

class Supervisor:
    def __init__(self, ctx, clock):
        self.ctx, self.clock = ctx, clock
        self.proc = None
        self.lock = threading.Lock()
        self.first_ready = threading.Event()
        self.events = []          # (t or None, what)
        self.stopping = False
        self.expected_exit = False
        self.starts = 0
        self.stderr = open(os.path.join(LOG_DIR, "relay.stderr.log"), "ab")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "OUTBOX_", "SEAM_"))}
        self.env.update({
            "PGHOST": ctx["host"], "PGPORT": str(ctx["port"]), "PGDATABASE": "ledger",
            "PGUSER": "outbox_relay", "PGPASSWORD": ctx["pw"]["outbox_relay"],
            "OUTBOX_QUEUE_URL": ctx["queue_url"],
            "AWS_ENDPOINT_URL": os.environ.get("AWS_ENDPOINT_URL", "http://engines:4566"),
            "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
            "PYTHONPATH": "/app", "PYTHONUNBUFFERED": "1",
        })

    def _t(self):
        return None if self.clock.t0 is None else round(self.clock.now(), 3)

    def start(self):
        with self.lock:
            work = tempfile.mkdtemp(prefix="relay-")
            env = dict(self.env, HOME=work, TMPDIR=work)
            self.stderr.write(f"\n===== start {self.starts} at t={self._t()} =====\n".encode())
            self.stderr.flush()
            self.proc = subprocess.Popen(["python3", "/app/relay.py"], cwd=work, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=self.stderr, start_new_session=True)
            self.expected_exit = False
            self.starts += 1
            proc = self.proc
            self.events.append((self._t(), f"start pid={proc.pid}"))
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()

    def _read(self, proc):
        for raw in proc.stdout:
            if raw.strip() == b"READY":
                if self.clock.t0 is None:
                    self.clock.t0 = time.monotonic()
                self.events.append((self._t(), f"READY pid={proc.pid}"))
                self.first_ready.set()

    def _watch(self, proc):
        rc = proc.wait()
        with self.lock:
            if proc is not self.proc or self.expected_exit or self.stopping:
                return
            self.events.append((self._t(), f"exited on its own rc={rc}; supervisor restarts it"))
        time.sleep(RESTART_DELAY)
        if not self.stopping:
            self.start()

    def kill(self):
        with self.lock:
            proc, self.expected_exit = self.proc, True
            self.events.append((self._t(), f"SIGKILL pid={proc.pid}"))
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait()

    def terminate(self):
        with self.lock:
            self.stopping = True
            proc = self.proc
            self.events.append((self._t(), "SIGTERM"))
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
            self.events.append((self._t(), f"exited rc={proc.returncode}"))
        except (ProcessLookupError, subprocess.TimeoutExpired):
            self.events.append((self._t(), "no exit within 5 s of SIGTERM; killed"))
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ---------------------------------------------------------------- queue drain

class Drain:
    def __init__(self, ctx, clock):
        self.url, self.clock = ctx["queue_url"], clock
        self.sqs = boto3.client("sqs")
        self.records = []        # (t, message_id, position_attr, body)
        self.lock = threading.Lock()
        self.stop_after_quiet = False
        self.errors = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        quiet = 0
        while True:
            try:
                resp = self.sqs.receive_message(QueueUrl=self.url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                                                MessageAttributeNames=["All"], AttributeNames=["All"])
            except Exception as exc:  # keep draining
                self.errors.append(repr(exc))
                time.sleep(0.5)
                continue
            msgs = resp.get("Messages", [])
            t = self.clock.now() if self.clock.t0 is not None else -1.0
            if not msgs:
                quiet += 1
                if self.stop_after_quiet and quiet >= 3:
                    return
                continue
            quiet = 0
            with self.lock:
                for m in msgs:
                    attrs = m.get("MessageAttributes", {})
                    mid = attrs.get("message_id", {}).get("StringValue") or m.get("Attributes", {}).get("MessageDeduplicationId")
                    pos = attrs.get("position", {}).get("StringValue")
                    self.records.append((t, mid, pos, m.get("Body"), m.get("Attributes", {}).get("MessageGroupId")))
            try:
                self.sqs.delete_message_batch(QueueUrl=self.url, Entries=[
                    {"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]} for i, m in enumerate(msgs)])
            except Exception as exc:
                self.errors.append(repr(exc))

    def received_ids(self, by=None):
        with self.lock:
            return {r[1] for r in self.records if by is None or r[0] <= by}


# ---------------------------------------------------------------- writers

class Writers:
    def __init__(self, ctx, clock):
        self.ctx, self.clock = ctx, clock
        self.rows = []            # dict(position, message_id, xid, committed_at, kind)
        self.facts = {}
        self.errors = []
        self.lock = threading.Lock()
        self.run_id = uuid.uuid4().hex[:8]

    def _conn(self):
        return connect(self.ctx, "app_writer", autocommit=False, app="ledger-api")

    @staticmethod
    def _xid(conn):
        return int(conn.execute("select pg_current_xact_id()::text").fetchone()[0])

    @staticmethod
    def _insert(conn, mid, mtype, data):
        row = conn.execute("insert into outbox (message_id, message_type, data) values (%s, %s, %s::jsonb) "
                           "on conflict (message_id) do nothing returning position",
                           (mid, mtype, json.dumps(data))).fetchone()
        return None if row is None else row[0]

    def _record(self, **row):
        with self.lock:
            self.rows.append(row)

    def _guard(self, name, fn):
        def run():
            try:
                fn()
            except Exception:
                self.errors.append(f"{name}: {traceback.format_exc()}")
        t = threading.Thread(target=run, name=name, daemon=True)
        return t

    def background(self, w):
        conn = self._conn()
        rng = random.Random(f"{self.run_id}-{w}")
        self.clock.sleep_until(w * BG_STAGGER_MS / 1000.0)
        n = 0
        while self.clock.now() < BG_STOP_AT:
            life = rng.uniform(*BG_TXN_MS) / 1000.0
            ins = rng.uniform(0, life)
            begin = time.monotonic()
            xid = self._xid(conn)
            time.sleep(ins)
            mid = f"pay-{self.run_id}-{w}-{n}"
            pos = self._insert(conn, mid, "PaymentCaptured", {"payment": mid, "amount": rng.randint(1, 99999)})
            time.sleep(max(0.0, life - (time.monotonic() - begin)))
            conn.commit()
            self._record(position=pos, message_id=mid, xid=xid, committed_at=self.clock.now(), kind="bg")
            n += 1
            time.sleep(rng.uniform(0, BG_GAP_MAX_MS / 1000.0))
        self.facts[f"bg{w}_rows"] = n

    def bystander(self, conn):
        self.clock.sleep_until(BYSTANDER_OPEN_AT)
        conn.execute("select pg_advisory_xact_lock(%s)", (BYSTANDER_LOCK_KEY,))
        self.facts["bystander_pid"] = conn.info.backend_pid
        # stays idle in transaction, without an xid, until the scenario closes it

    def rollback_writer(self):
        conn = self._conn()
        self.clock.sleep_until(ROLLBACK_DRAW_AT)
        xid = self._xid(conn)
        self.facts["r1"] = self._insert(conn, f"adj-{self.run_id}-r", "BalanceAdjusted", {"adj": 1})
        self.facts["r1_xid"] = xid
        self.clock.sleep_until(ROLLBACK_AT)
        conn.rollback()
        self.facts["r1_dead_at"] = self.clock.now()

    def burn_writer(self):
        conn = self._conn()
        self.clock.sleep_until(BURN_AT)
        self._xid(conn)
        dup = f"pay-{self.run_id}-0-3"     # written by background writer 0 well before this
        got = self._insert(conn, dup, "PaymentCaptured", {"payment": dup, "retry": True})
        r2 = conn.execute("select currval('outbox_position_seq')").fetchone()[0]
        conn.commit()
        self.facts["r2"] = r2
        self.facts["r2_dropped"] = got is None
        self.facts["r2_dead_at"] = self.clock.now()

    def slow_writer(self):
        conn = self._conn()
        self.clock.sleep_until(SLOW_DRAW_AT)
        xid = self._xid(conn)
        mid = f"recon-{self.run_id}"
        pos = self._insert(conn, mid, "ReconciliationStarted", {"run": mid})
        self.facts.update(p_s=pos, p_s_xid=xid)
        self.clock.sleep_until(SLOW_COMMIT_AT)
        conn.commit()
        self.facts["p_s_committed"] = True
        self._record(position=pos, message_id=mid, xid=xid, committed_at=self.clock.now(), kind="slow")

    def pair(self, times=(PAIR_A_XID_AT, PAIR_B_XID_AT, PAIR_B_INSERT_AT, PAIR_A_INSERT_AT,
                          PAIR_A_COMMIT_AT, PAIR_B_COMMIT_AT), tag=""):
        a_xid_at, b_xid_at, b_insert_at, a_insert_at, a_commit_at, b_commit_at = times
        a, b = self._conn(), self._conn()
        self.clock.sleep_until(a_xid_at)
        xa = self._xid(a)
        self.clock.sleep_until(b_xid_at)
        xb = self._xid(b)
        self.clock.sleep_until(b_insert_at)
        mb = f"stmt-{self.run_id}-b{tag}"
        pb = self._insert(b, mb, "StatementIssued", {"stmt": mb})
        self.clock.sleep_until(a_insert_at)
        ma = f"stmt-{self.run_id}-a{tag}"
        pa = self._insert(a, ma, "StatementIssued", {"stmt": ma})
        self.facts.update({f"p_a{tag}": pa, f"p_b{tag}": pb, f"xa{tag}": xa, f"xb{tag}": xb})
        self.clock.sleep_until(a_commit_at)
        a.commit()
        self._record(position=pa, message_id=ma, xid=xa, committed_at=self.clock.now(), kind=f"pair_a{tag}")
        self.clock.sleep_until(b_commit_at)
        b.commit()
        self.facts[f"p_b{tag}_committed"] = True
        self._record(position=pb, message_id=mb, xid=xb, committed_at=self.clock.now(), kind=f"pair_b{tag}")


# ---------------------------------------------------------------- the run

def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    result = {"environment_error": None, "relay_ready": False}
    try:
        ctx = provision()
    except EnvironmentFailure as exc:
        result["environment_error"] = str(exc)
        open(os.path.join(LOG_DIR, "environment"), "w").write(str(exc) + "\n")
        return result
    admin = ctx["admin"]
    clock = Clock()
    drain = Drain(ctx, clock)
    drain.thread.start()
    writers = Writers(ctx, clock)
    bystander_conn = connect(ctx, "app_writer", autocommit=False, app="ledger-scheduler")
    sup = Supervisor(ctx, clock)
    sup.start()
    if not sup.first_ready.wait(FIRST_READY_TIMEOUT):
        sup.terminate()
        result["supervisor"] = sup.events
        _log("relay never printed READY")
        return result
    result["relay_ready"] = True
    _log("T0: relay READY")

    threads = [writers._guard(f"bg{w}", lambda w=w: writers.background(w)) for w in range(BG_WRITERS)]
    threads += [writers._guard("bystander", lambda: writers.bystander(bystander_conn)),
                writers._guard("rollback", writers.rollback_writer),
                writers._guard("burn", writers.burn_writer),
                writers._guard("slow", writers.slow_writer),
                writers._guard("pair", writers.pair),
                writers._guard("pair2", lambda: writers.pair(PAIR2_TIMES, "2"))]
    for t in threads:
        t.start()

    def chaos():
        for k in KILL_TIMES:
            clock.sleep_until(k)
            sup.kill()
            time.sleep(RESTART_DELAY)
            sup.start()
    threading.Thread(target=chaos, daemon=True).start()

    def checkpoint():
        return admin.execute("select position from relay_checkpoint where id = 1").fetchone()[0]

    samples = []

    # phase 1: dead holes passed
    clock.sleep_until(PHASE1_AT)
    f28 = checkpoint()
    samples.append((PHASE1_AT, f28))
    clock.sleep_until(PHASE1_AT + PHASE1_GRACE)
    f_p1 = checkpoint()
    samples.append((round(clock.now(), 2), f_p1))
    with writers.lock:
        due = [r for r in writers.rows if r["committed_at"] < PHASE1_COMMITTED_BEFORE]
    got = drain.received_ids(by=PHASE1_AT + PHASE1_GRACE)
    result["phase1"] = {"checkpoint_at_28": f28, "checkpoint": f_p1, "due": len(due),
                        "missing": sorted(r["position"] for r in due if r["message_id"] not in got)}
    _log(f"phase1 {json.dumps({k: v if k != 'missing' else v[:20] for k, v in result['phase1'].items()})}")

    # phase 2: live reservation held (one statement, one snapshot)
    clock.sleep_until(PHASE2_AT)
    p_s, p_s_xid = writers.facts.get("p_s"), writers.facts.get("p_s_xid")
    in_xip, present, f60 = admin.execute(
        "select %s::text::xid8 = any(array(select pg_snapshot_xip(pg_current_snapshot()))), "
        "exists(select 1 from outbox where position = %s), (select position from relay_checkpoint where id = 1)",
        (str(p_s_xid or 0), p_s or -1)).fetchone()
    samples.append((PHASE2_AT, f60))
    clock.sleep_until(PHASE2_AT + 1.0)
    with writers.lock:
        below = [r for r in writers.rows if p_s is not None and r["position"] < p_s and r["committed_at"] < PHASE2_AT - 5]
    got = drain.received_ids(by=PHASE2_AT + 1.0)
    result["phase2"] = {"p_s": p_s, "slow_xid_in_xip": in_xip, "p_s_row_present": present, "checkpoint": f60,
                        "missing_below_p_s": sorted(r["position"] for r in below if r["message_id"] not in got)}
    clock.sleep_until(PHASE2_LATE_AT)
    f74 = checkpoint()
    samples.append((PHASE2_LATE_AT, f74))
    result["phase2"]["checkpoint_at_74"] = f74
    _log(f"phase2 {json.dumps({k: v if not isinstance(v, list) else v[:20] for k, v in result['phase2'].items()})}")

    # second pair: B2 still open, no newer xid completing
    clock.sleep_until(PAIR2_CHECK_AT)
    f110 = checkpoint()
    samples.append((PAIR2_CHECK_AT, f110))
    result["pair2"] = {"p_b2": writers.facts.get("p_b2"), "checkpoint": f110}

    # final
    clock.sleep_until(FINAL_AT)
    for t in threads:
        t.join(timeout=5)
    head = admin.execute("select max(position) from outbox").fetchone()[0]
    deadline = time.monotonic() + FINAL_CATCHUP
    f_final = checkpoint()
    while f_final != head and time.monotonic() < deadline:
        time.sleep(0.5)
        f_final = checkpoint()
    samples.append((round(clock.now(), 2), f_final))
    sup.terminate()
    drain.stop_after_quiet = True
    drain.thread.join(timeout=60)
    try:
        bystander_conn.rollback()
    except Exception:
        pass

    db_rows = admin.execute("select position, message_id, data::text from outbox where position > %s order by position",
                            (INITIAL_CHECKPOINT,)).fetchall()
    hist_ids = {f"hist-{n}" for n in range(1, PRESEED_ROWS + 1)}
    sqs = boto3.client("sqs")
    qattrs = sqs.get_queue_attributes(QueueUrl=ctx["queue_url"], AttributeNames=["All"])["Attributes"]
    slots = admin.execute("select count(*) from pg_replication_slots").fetchone()[0]

    result.update({
        "facts": writers.facts, "writer_errors": writers.errors, "drain_errors": drain.errors[:20],
        "head": head, "checkpoint_final": f_final, "samples": samples,
        "supervisor": sup.events,
        "db_rows": [(p, m, d) for p, m, d in db_rows],
        "hist_ids_received": sorted(i for i in drain.received_ids() if i in hist_ids)[:50],
        "received": [list(r) for r in drain.records],
        "bg_rows": sum(1 for r in writers.rows if r["kind"] == "bg"),
        "queue_attributes": {k: qattrs.get(k) for k in ("FifoQueue", "ContentBasedDeduplication", "DelaySeconds")},
        "replication_slots": slots,
    })
    return result


_CACHE = {}


def get():
    if "r" not in _CACHE:
        r = run()
        _CACHE["r"] = r
        slim = {k: v for k, v in r.items() if k not in ("db_rows", "received")}
        slim["received_count"] = len(r.get("received", []))
        slim["db_row_count"] = len(r.get("db_rows", []))
        with open(os.path.join(LOG_DIR, "scenario.json"), "w") as fh:
            json.dump(slim, fh, indent=1, default=str)
        with open(os.path.join(LOG_DIR, "received.jsonl"), "w") as fh:
            for rec in r.get("received", []):
                fh.write(json.dumps(rec) + "\n")
    return _CACHE["r"]
