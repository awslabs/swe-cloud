"""Verifier harness: resources, a supervised worker pool behind fault proxies, and state reads.

Nothing here imports code from /app. Workers are black boxes started as
`python3 /app/compose_worker.py --worker-id <id>`; every AWS call they make goes
through a per-worker HTTP relay (their AWS_ENDPOINT_URL), which forwards to the
emulator and can SIGKILL the worker right after a committed write, before the
reply reaches it, or freeze it there (SIGSTOP, the way a hibernated spot instance
stops) and let it resume later with that reply. A consumer stands in for the
user-review service on its queue; `Stack.take_down` does what moderation does.
"""
import contextlib
import http.client
import http.server
import json
import os
import random
import re
import signal
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
from collections import Counter

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

FLOCI_HOST, FLOCI_PORT = "localhost", 4566
FLOCI = f"http://{FLOCI_HOST}:{FLOCI_PORT}"
APP = os.environ.get("REELNOTES_APP", "/app")
LOG_ROOT = os.environ.get("VERIFIER_LOG_DIR", "/logs/verifier")

PARTS = ("unique_id", "text", "rating", "user_id", "movie_id")
N_PARTS = 5
VISIBILITY_TIMEOUT_S = 6
WORKERS = 3
READY_TIMEOUT_S = 20
RESPAWN_DELAY_S = 1
ARRIVAL_STEP_TIMEOUT_S = 5
REQUEST_DEADLINE_S = 30
QUIESCE_S = 12
MAX_KILLS_PER_MESSAGE = 2

DDB_WRITE_OPS = {
    "PutItem", "UpdateItem", "DeleteItem", "TransactWriteItems", "BatchWriteItem",
    "ExecuteStatement", "BatchExecuteStatement", "ExecuteTransaction",
}
HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "content-length", "te", "trailer", "upgrade"}


def aws(service):
    return boto3.client(
        service,
        endpoint_url=FLOCI,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"max_attempts": 6, "mode": "standard"}, connect_timeout=5, read_timeout=30),
    )


def wait_until(predicate, timeout, interval=0.1):
    deadline = time.time() + timeout
    while True:
        try:
            if predicate():
                return True
        except ClientError:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def emulator_live():
    try:
        conn = http.client.HTTPConnection(FLOCI_HOST, FLOCI_PORT, timeout=5)
        conn.request("GET", "/_floci/health")
        ok = conn.getresponse().status == 200
        conn.close()
        return ok
    except OSError:
        return False


# --------------------------------------------------------------------------- resources

class Request:
    def __init__(self, req_id, movie_id, values):
        self.req_id = req_id
        self.movie_id = movie_id
        self.values = values
        self.order = []
        self.last_enqueue = None
        self.notes = []

    @property
    def review_id(self):
        return self.values["unique_id"]


class Stack:
    """One isolated deployment: queue + frontend tables, fresh names."""

    def __init__(self, tag):
        sfx = uuid.uuid4().hex[:8]
        self.tag = tag
        self.ddb = aws("dynamodb")
        self.sqs = aws("sqs")
        self.t_compose = f"review_compose_{sfx}"
        self.t_responses = f"responses_{sfx}"
        self.t_reviews = f"reviews_store_{sfx}"
        self.t_movies = f"movie_reviews_{sfx}"
        self.t_idem = f"compose_idempotency_{sfx}"
        tables = {
            self.t_compose: [("req_id", "HASH")],
            self.t_responses: [("req_id", "HASH"), ("part", "RANGE")],
            self.t_reviews: [("review_id", "HASH")],
            self.t_movies: [("movie_id", "HASH")],
            self.t_idem: [("id", "HASH")],
        }
        self.user_review_queue_name = f"user-review-uploads-{sfx}"
        outputs = deploy(aws("cloudformation"), "review-" + sfx,
                         Path(__file__).parent / "cdk_seed/seed.template.json",
                         ComposeTable=self.t_compose, ResponsesTable=self.t_responses,
                         ReviewsTable=self.t_reviews, MoviesTable=self.t_movies,
                         IdempotencyTable=self.t_idem, UploadQueue=f"upload-parts-{sfx}",
                         UserReviewQueue=self.user_review_queue_name)
        for name in tables:
            wait_until(lambda: self.ddb.describe_table(TableName=name)["Table"]["TableStatus"] == "ACTIVE", 30)
        self.queue_url = outputs["UploadUrl"]
        self.user_review_queue_url = outputs["UserReviewUrl"]
        self.handoffs = []        # every message the user-review service processed, in order
        self.requests = []
        self.movies = set()
        self.taken_down = set()   # review ids moderation removed from their movie page

    def worker_env(self):
        return {
            "QUEUE_URL": self.queue_url,
            "USER_REVIEW_QUEUE_URL": self.user_review_queue_url,
            "TABLE_COMPOSE": self.t_compose,
            "TABLE_RESPONSES": self.t_responses,
            "TABLE_REVIEWS": self.t_reviews,
            "TABLE_MOVIES": self.t_movies,
            "TABLE_IDEMPOTENCY": self.t_idem,
        }

    # frontend ---------------------------------------------------------------
    def new_request(self, movie_id=None):
        """The frontend's UploadReq step, with fresh random part values."""
        movie_id = movie_id or f"tt{random.randint(1000000, 9999999)}"
        req_id = f"req-{uuid.uuid4().hex[:16]}"
        values = {
            "unique_id": f"rv-{uuid.uuid4().hex[:16]}",
            "text": f"text-{uuid.uuid4().hex}",
            "rating": str(random.randint(1, 10)),
            "user_id": f"user-{uuid.uuid4().hex[:10]}",
            "movie_id": movie_id,
        }
        self.ddb.put_item(TableName=self.t_compose, Item={"req_id": {"S": req_id}, "counter": {"N": "0"}})
        if movie_id not in self.movies:
            try:
                self.ddb.put_item(
                    TableName=self.t_movies,
                    Item={"movie_id": {"S": movie_id}, "review_ids": {"L": []}},
                    ConditionExpression="attribute_not_exists(movie_id)",
                )
            except ClientError as err:
                if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
            self.movies.add(movie_id)
        req = Request(req_id, movie_id, values)
        self.requests.append(req)
        return req

    def resubmit(self, req):
        """The frontend's resubmission of a slow submission: a new request carrying the same values."""
        new = self.new_request(req.movie_id)
        new.values = dict(req.values)
        return new

    def send(self, req, part):
        body = {"req_id": req.req_id, "part": part, "value": req.values[part], "movie_id": req.movie_id}
        self.sqs.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(body), DelaySeconds=0)
        req.last_enqueue = time.time()

    def take_down(self, req):
        """Moderation's takedown: remove the review id from its movie page (the stored review stays).
        Only a listed review can be taken down; returns whether it was."""
        key = {"movie_id": {"S": req.movie_id}}
        while True:
            ids = self.review_ids(req.movie_id)
            if req.review_id not in ids:
                req.notes.append(f"takedown: {req.review_id} was not listed, nothing taken down")
                return False
            i = ids.index(req.review_id)
            try:
                self.ddb.update_item(
                    TableName=self.t_movies, Key=key,
                    UpdateExpression=f"REMOVE review_ids[{i}]",
                    ConditionExpression=f"review_ids[{i}] = :id",
                    ExpressionAttributeValues={":id": {"S": req.review_id}},
                )
            except ClientError as err:
                if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                continue
            if req.review_id in self.review_ids(req.movie_id):
                continue                                   # it was listed more than once
            self.taken_down.add(req.review_id)
            req.notes.append(f"takedown: moderation removed {req.review_id} from {req.movie_id}")
            return True

    def expected_listings(self, req):
        return 0 if req.review_id in self.taken_down else 1

    # reads ------------------------------------------------------------------
    def compose_item(self, req_id):
        return self.ddb.get_item(
            TableName=self.t_compose, Key={"req_id": {"S": req_id}}, ConsistentRead=True
        ).get("Item")

    def counter(self, req_id):
        item = self.compose_item(req_id)
        if not item or "counter" not in item:
            return None
        try:
            return int(item["counter"]["N"])
        except (KeyError, ValueError):
            return None

    def replies(self, req_id):
        items, kwargs = [], {}
        while True:
            out = self.ddb.query(
                TableName=self.t_responses,
                KeyConditionExpression="req_id = :r",
                ExpressionAttributeValues={":r": {"S": req_id}},
                ConsistentRead=True,
                **kwargs,
            )
            items += out["Items"]
            if "LastEvaluatedKey" not in out:
                return items
            kwargs = {"ExclusiveStartKey": out["LastEvaluatedKey"]}

    def reply_exists(self, req_id, part):
        return "Item" in self.ddb.get_item(
            TableName=self.t_responses,
            Key={"req_id": {"S": req_id}, "part": {"S": part}},
            ConsistentRead=True,
        )

    def stored_review(self, review_id):
        return self.ddb.get_item(
            TableName=self.t_reviews, Key={"review_id": {"S": review_id}}, ConsistentRead=True
        ).get("Item")

    def all_reviews(self):
        items, kwargs = [], {}
        while True:
            out = self.ddb.scan(TableName=self.t_reviews, ConsistentRead=True, **kwargs)
            items += out["Items"]
            if "LastEvaluatedKey" not in out:
                return items
            kwargs = {"ExclusiveStartKey": out["LastEvaluatedKey"]}

    def review_ids(self, movie_id):
        item = self.ddb.get_item(
            TableName=self.t_movies, Key={"movie_id": {"S": movie_id}}, ConsistentRead=True
        ).get("Item") or {}
        return [v.get("S") for v in item.get("review_ids", {}).get("L", [])]

    def review_stored(self, review_id):
        return "Item" in self.ddb.get_item(
            TableName=self.t_reviews, Key={"review_id": {"S": review_id}}, ConsistentRead=True
        )

    def handed_off(self, req):
        """True once the user-review service accepted a message carrying this review's values."""
        want = {"review_id": req.review_id, "user_id": req.values["user_id"],
                "movie_id": req.movie_id, "rating": req.values["rating"]}
        return any(h["stored"] and all(h["body"].get(k) == v for k, v in want.items())
                   for h in list(self.handoffs))

    def queue_state(self):
        return self.sqs.get_queue_attributes(QueueUrl=self.queue_url, AttributeNames=["All"])["Attributes"]

    def snapshot(self, req):
        """The graded projection of one request's state."""
        item = self.compose_item(req.req_id)
        if item is not None:
            item = {k: next(iter(v.values()), None) if isinstance(v, dict) and len(v) == 1 else repr(v)
                    for k, v in sorted(item.items())}
        replies = {}
        for row in self.replies(req.req_id):
            rank = row.get("rank", {})
            rank = rank.get("N", rank.get("S"))
            try:
                rank = int(rank)
            except (TypeError, ValueError):
                pass
            composed = row.get("composed", {}).get("BOOL")
            replies[row["part"]["S"]] = (rank, composed)
        stored = self.stored_review(req.review_id)
        review = None
        if stored is not None:
            # Which request's compose stored it is not part of the projection: a resubmitted
            # review is carried by two requests.
            review = {k: (v.get("S") if isinstance(v, dict) else v) for k, v in stored.items()
                      if k in PARTS + ("review_id",)}
        on_movie = self.review_ids(req.movie_id).count(req.review_id)
        return {"compose_item": item, "replies": replies, "review": review, "on_movie": on_movie,
                "handed_off": self.handed_off(req)}

    def is_complete(self, req):
        if len(self.replies(req.req_id)) < N_PARTS:
            return False
        if self.stored_review(req.review_id) is None:
            return False
        if self.expected_listings(req) and req.review_id not in self.review_ids(req.movie_id):
            return False
        if not self.handed_off(req):
            return False
        return self.compose_item(req.req_id) is None


class UserReviewService:
    """Consumes the user-review queue the way that service does: on processing a message it reads
    the review from reviews_store and discards the message if the review is not there."""

    def __init__(self, stack):
        self.stack = stack
        self.sqs = aws("sqs")
        self.ddb = aws("dynamodb")
        self.stopping = threading.Event()
        self.t0 = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.thread.join(10)

    def _run(self):
        url = self.stack.user_review_queue_url
        while not self.stopping.is_set():
            try:
                out = self.sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
            except ClientError:
                time.sleep(0.2)
                continue
            for m in out.get("Messages", []):
                try:
                    body = json.loads(m["Body"])
                    if not isinstance(body, dict):
                        body = {"_raw": m["Body"]}
                except ValueError:
                    body = {"_raw": m["Body"]}
                rid = body.get("review_id")
                stored = isinstance(rid, str) and self.stack.review_stored(rid)
                self.stack.handoffs.append({"t": round(time.time() - self.t0, 3), "body": body, "stored": stored})
                self.sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])


# --------------------------------------------------------------------------- fault proxy

def _op_and_service(headers, body):
    target = headers.get("X-Amz-Target", "") or ""
    if target.startswith("DynamoDB"):
        return "dynamodb", target.rsplit(".", 1)[-1]
    if target.startswith("AmazonSQS"):
        return "sqs", target.rsplit(".", 1)[-1]
    if body and b"Action=" in body:
        params = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        action = (params.get("Action") or [""])[0]
        return "sqs-query", action
    return "other", ""


def _json(data):
    try:
        return json.loads(data or b"{}")
    except ValueError:
        return {}


def tables_in_write(op, body):
    """Table names a DynamoDB write request touches (JSON protocol)."""
    req = _json(body)
    names = set()
    if isinstance(req.get("TableName"), str):
        names.add(req["TableName"])
    for entry in req.get("TransactItems", []) or []:
        for v in entry.values():
            if isinstance(v, dict) and isinstance(v.get("TableName"), str):
                names.add(v["TableName"])
    for name in (req.get("RequestItems") or {}):
        names.add(name)
    statements = []
    if isinstance(req.get("Statement"), str):
        statements.append(req["Statement"])
    for entry in (req.get("Statements") or req.get("TransactStatements") or []):
        if isinstance(entry, dict) and isinstance(entry.get("Statement"), str):
            statements.append(entry["Statement"])
    for st in statements:
        names.update(re.findall(r'"([^"]+)"', st))
        names.update(re.findall(r"\b([A-Za-z0-9_.-]+)\b", st))
    return names


class Slot:
    def __init__(self, index):
        self.index = index
        self.port = None
        self.proc = None
        self.worker_id = None
        self.generation = 0
        self.ready = threading.Event()
        self.held = {}            # MessageId -> ReceiptHandle
        self.killed_by_us = False
        self.dead_since = None
        self.frozen = False
        self.thawed = threading.Event()
        self.lock = threading.RLock()


class Controller:
    """Decides, per relayed call, whether the calling worker dies before seeing the reply."""

    def __init__(self, pool):
        self.pool = pool
        self.lock = threading.Lock()
        self.serial = threading.Lock()
        self.mode = None
        self.action = "kill"
        self.req_id = None
        self.watch_tables = set()
        self.fired = threading.Event()
        self.fired_info = None
        self.p = 0.0
        self.rng = random.Random()
        self.receipts = {}        # ReceiptHandle -> MessageId
        self.kills = Counter()    # MessageId -> kills charged
        self.ddb = aws("dynamodb")

    def arm(self, mode, req_id, action="kill"):
        with self.lock:
            self.mode, self.req_id, self.action = mode, req_id, action
            self.fired.clear()
            self.fired_info = None

    def arm_chaos(self, p, seed):
        with self.lock:
            self.mode, self.req_id, self.p, self.action = "chaos", None, p, "kill"
            self.rng = random.Random(seed)
            self.fired.clear()

    def disarm(self):
        with self.lock:
            self.mode = None

    def _compose_state(self, req_id):
        """(item exists, counter) for the armed request; None when unreadable."""
        try:
            item = self.ddb.get_item(
                TableName=self.pool.stack.t_compose, Key={"req_id": {"S": req_id}}, ConsistentRead=True
            ).get("Item")
            if item is None:
                return (False, None)
            return (True, int(item.get("counter", {}).get("N", "0")))
        except (ClientError, ValueError):
            return None

    def _is_handoff(self, service, op, path, body):
        if not service.startswith("sqs") or op not in ("SendMessage", "SendMessageBatch"):
            return False
        name = self.pool.stack.user_review_queue_name
        return name in urllib.parse.unquote(path) or name in urllib.parse.unquote(body.decode("utf-8", "replace"))

    def relay(self, slot, method, path, headers, body):
        service, op = _op_and_service(headers, body)
        if service.startswith("sqs") and op == "ReceiveMessage":
            slot.ready.set()
        mode, req_id, action = self.mode, self.req_id, self.action
        watched = mode in ("counter", "cleanup") and service == "dynamodb"
        serial = self.serial if watched else contextlib.nullcontext()
        kill_reason = None
        with serial:
            before = self._compose_state(req_id) if watched else None
            status, reason, resp_headers, data = forward(method, path, headers, body)
            if service.startswith("sqs"):
                self._track_sqs(slot, service, op, body, data, status)
            if service == "dynamodb" and status == 200:
                kill_reason = self._decide(slot, op, body, before)
            elif status == 200 and self._is_handoff(service, op, path, body):
                kill_reason = self._decide_handoff(slot, op)
        if kill_reason and action == "freeze":
            # The worker stops before it reads the reply; the reply is delivered when it resumes.
            self.pool.freeze(slot, kill_reason)
            slot.thawed.wait()
            return status, reason, resp_headers, data, lambda: self.pool.resume(slot)
        if kill_reason:
            self.pool.kill(slot, kill_reason)
            return None
        return status, reason, resp_headers, data, None

    def _decide(self, slot, op, body, before):
        with self.lock:
            mode = self.mode
            if mode in ("counter", "cleanup"):
                if before is None or not before[0]:
                    return None
                after = self._compose_state(self.req_id)
                if after is None:
                    return None
                vanished = not after[0]
                # A write that removes the item also completes the count, whatever form it takes.
                if vanished or (mode == "counter" and after[1] > before[1]):
                    self.mode = None
                    self.fired_info = {"op": op, "counter_before": before[1],
                                       "counter_after": "item deleted" if vanished else after[1],
                                       "worker": slot.worker_id}
                    self.fired.set()
                    return f"after {mode} write {op} ({before[1]}->{self.fired_info['counter_after']}) on {self.req_id}"
            elif mode == "compose" and op in DDB_WRITE_OPS:
                stack = self.pool.stack
                hit = tables_in_write(op, body) & {stack.t_reviews, stack.t_movies}
                if hit:
                    self.mode = None
                    self.fired_info = {"op": op, "tables": sorted(hit), "worker": slot.worker_id}
                    self.fired.set()
                    return f"after compose write {op} on {sorted(hit)}"
            elif mode == "chaos" and op in DDB_WRITE_OPS:
                return self._chaos(slot, op)
        return None

    def _decide_handoff(self, slot, op):
        with self.lock:
            if self.mode == "chaos":
                return self._chaos(slot, f"{op} to the user-review queue")
        return None

    def _chaos(self, slot, what):
        held = list(slot.held)
        if held and all(self.kills[m] < MAX_KILLS_PER_MESSAGE for m in held) and self.rng.random() < self.p:
            for m in held:
                self.kills[m] += 1
            return f"chaos after {what} holding {len(held)} message(s)"
        return None

    def _track_sqs(self, slot, service, op, body, data, status):
        if status != 200:
            return
        with slot.lock:
            if op == "ReceiveMessage":
                if service == "sqs":
                    msgs = [(m.get("MessageId"), m.get("ReceiptHandle")) for m in _json(data).get("Messages", []) or []]
                else:
                    text = data.decode("utf-8", "replace")
                    msgs = [(re.search(r"<MessageId>(.*?)</MessageId>", blk, re.S).group(1),
                             re.search(r"<ReceiptHandle>(.*?)</ReceiptHandle>", blk, re.S).group(1))
                            for blk in re.findall(r"<Message>(.*?)</Message>", text, re.S)]
                for mid, rh in msgs:
                    if mid and rh:
                        slot.held[mid] = rh
                        self.receipts[rh] = mid
            elif op in ("DeleteMessage", "DeleteMessageBatch"):
                handles = []
                if service == "sqs":
                    req = _json(body)
                    if req.get("ReceiptHandle"):
                        handles.append(req["ReceiptHandle"])
                    handles += [e.get("ReceiptHandle") for e in req.get("Entries", []) or []]
                else:
                    params = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                    handles += [v[0] for k, v in params.items() if k.endswith("ReceiptHandle")]
                for rh in handles:
                    mid = self.receipts.get(rh)
                    if mid:
                        slot.held.pop(mid, None)


def forward(method, path, headers, body):
    conn = http.client.HTTPConnection(FLOCI_HOST, FLOCI_PORT, timeout=90)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            if k.lower() in HOP_HEADERS:
                continue
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(body)))
        conn.putheader("Connection", "close")
        conn.endheaders(body)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, resp.reason, resp.getheaders(), data
    finally:
        conn.close()


class _ProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass                                   # a worker killed mid-call drops its socket


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _relay(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            result = self.server.controller.relay(self.server.slot, self.command, self.path, self.headers, body)
        except OSError:
            self.close_connection = True
            return
        if result is None:
            self.close_connection = True
            return
        status, reason, headers, data, after = result
        try:
            self.send_response_only(status, reason)
            for k, v in headers:
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            self.wfile.flush()
        finally:
            if after is not None:
                after()

    do_POST = do_GET = do_PUT = do_DELETE = do_HEAD = _relay


# --------------------------------------------------------------------------- pool

class InterfaceError(AssertionError):
    pass


class Pool:
    """WORKERS supervised workers, each behind its own relay port."""

    def __init__(self, stack, logdir):
        self.stack = stack
        self.logdir = logdir
        os.makedirs(logdir, exist_ok=True)
        self.controller = Controller(self)
        self.user_reviews = UserReviewService(stack)
        self.slots = []
        self.servers = []
        self.events = []
        self.stopping = threading.Event()
        self.t0 = time.time()
        for _ in range(WORKERS):
            self._add_slot()
        self.monitor = threading.Thread(target=self._monitor, daemon=True)

    def _add_slot(self):
        slot = Slot(len(self.slots) + 1)
        server = _ProxyServer(("127.0.0.1", 0), _ProxyHandler)
        server.slot, server.controller = slot, self.controller
        slot.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        self.slots.append(slot)
        return slot

    def event(self, kind, **facts):
        self.events.append({"t": round(time.time() - self.t0, 3), "event": kind, **facts})

    def _spawn(self, slot):
        slot.generation += 1
        slot.worker_id = f"w{slot.index}-{slot.generation}"
        slot.ready.clear()
        slot.held = {}
        slot.killed_by_us = False
        slot.dead_since = None
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_") and not k.startswith("TABLE_")}
        env.update(self.stack.worker_env())
        env.update({
            "AWS_ENDPOINT_URL": f"http://127.0.0.1:{slot.port}",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "PYTHONPATH": APP,
            "PYTHONUNBUFFERED": "1",
        })
        cwd = tempfile.mkdtemp(prefix=f"{slot.worker_id}-")
        log = open(os.path.join(self.logdir, f"{slot.worker_id}.log"), "wb")
        slot.proc = subprocess.Popen(
            ["python3", os.path.join(APP, "compose_worker.py"), "--worker-id", slot.worker_id],
            cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        self.event("spawn", worker=slot.worker_id, pid=slot.proc.pid)

    def start(self):
        self.user_reviews.start()
        for slot in self.slots:
            self._spawn(slot)
        deadline = time.time() + READY_TIMEOUT_S
        for slot in self.slots:
            if not slot.ready.wait(max(0.0, deadline - time.time())):
                raise InterfaceError(
                    f"worker {slot.worker_id} never polled the queue within {READY_TIMEOUT_S}s "
                    f"(exit code {slot.proc.poll()}); see {self.logdir}/{slot.worker_id}.log")
        self.monitor.start()
        self.event("ready")

    def kill(self, slot, reason):
        with slot.lock:
            proc = slot.proc
            if proc is None or proc.poll() is not None:
                return
            slot.killed_by_us = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            slot.held = {}
            slot.dead_since = time.time()
        self.event("kill", worker=slot.worker_id, reason=reason)

    def freeze(self, slot, reason):
        """Hibernate the worker: stop its process group where it is; the pool manager sees the worker
        as lost and starts a replacement (a new slot) after RESPAWN_DELAY_S."""
        with slot.lock:
            proc = slot.proc
            if proc is None or proc.poll() is not None:
                return
            try:
                os.killpg(proc.pid, signal.SIGSTOP)
            except ProcessLookupError:
                return
            slot.frozen = True
        self.event("freeze", worker=slot.worker_id, reason=reason)

        def replace():
            if not self.stopping.is_set():
                self._spawn(self._add_slot())
        threading.Timer(RESPAWN_DELAY_S, replace).start()

    def thaw_all(self):
        """Let every frozen worker resume (it first receives the reply it was stopped before)."""
        for slot in list(self.slots):
            if slot.frozen:
                self.event("thaw", worker=slot.worker_id)
                slot.thawed.set()

    def resume(self, slot):
        with slot.lock:
            proc = slot.proc
            slot.frozen = False
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        self.event("resumed", worker=slot.worker_id)

    def _monitor(self):
        while not self.stopping.is_set():
            for slot in list(self.slots):
                with slot.lock:
                    proc = slot.proc
                    if proc is None or proc.poll() is None:
                        continue
                    if slot.dead_since is None:
                        slot.dead_since = time.time()
                        slot.held = {}
                        self.event("exit", worker=slot.worker_id, code=proc.returncode)
                    if time.time() - slot.dead_since >= RESPAWN_DELAY_S and not self.stopping.is_set():
                        self._spawn(slot)
            time.sleep(0.05)

    def kill_count(self):
        return sum(1 for e in self.events if e["event"] == "kill")

    def stop(self):
        self.stopping.set()
        self.controller.disarm()
        for slot in list(self.slots):
            proc = slot.proc
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            slot.thawed.set()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.user_reviews.stop()
        with open(os.path.join(self.logdir, "events.json"), "w") as fh:
            json.dump(self.events, fh, indent=1)
        with open(os.path.join(self.logdir, "user_review_messages.json"), "w") as fh:
            json.dump(self.stack.handoffs, fh, indent=1)


@contextlib.contextmanager
def deployment(tag, before_start=None):
    stack = Stack(tag)
    logdir = os.path.join(LOG_ROOT, tag)
    if before_start is not None:
        before_start(stack, logdir)
    pool = Pool(stack, logdir)
    try:
        pool.start()
        yield stack, pool
    finally:
        pool.stop()


# --------------------------------------------------------------------------- schedules and grading

def run_sequenced(stack, pool, kill=None, kill_at=None, hold_last=False, movie_id=None, req=None, start=1,
                  action="kill"):
    """Send one request's parts one at a time in a random order.

    Part a(i+1) is sent only once the counter shows i (or the planned kill has
    been observed), so the expected rank of a(i) is i. `kill` is "counter"
    (kill the worker right after the call that raised the counter or removed
    the item), "cleanup" (right after the call that removed the item) or
    "compose" (right after its first write to the review or movie table).
    `action` "freeze" stops the worker there instead of killing it (see Pool.freeze).
    `req`/`start` continue a request whose first start-1 parts are already in.
    """
    if req is None:
        req = stack.new_request(movie_id)
    if not req.order:
        req.order = random.sample(PARTS, N_PARTS)
    ctl = pool.controller
    for i, part in enumerate(req.order, start=1):
        if i < start:
            continue
        planned = kill is not None and i == kill_at
        if hold_last and i == N_PARTS:
            if not wait_until(lambda: stack.reply_exists(req.req_id, req.order[0]), REQUEST_DEADLINE_S, 0.2):
                req.notes.append(f"no reply for {req.order[0]} within {REQUEST_DEADLINE_S}s; sent the last part anyway")
        if planned:
            ctl.arm(kill, req.req_id, action)
        stack.send(req, part)
        if planned:
            if ctl.fired.wait(ARRIVAL_STEP_TIMEOUT_S):
                req.notes.append(f"planned {action} on part {i} ({part}): {ctl.fired_info}")
            else:
                ctl.disarm()
                req.notes.append(f"planned {action} on part {i} ({part}) never triggered")
        wait_until(lambda: stack.compose_item(req.req_id) is None or (stack.counter(req.req_id) or 0) >= i,
                   ARRIVAL_STEP_TIMEOUT_S)
    return req


def wait_drained(stack, timeout):
    """Wait until the upload queue has nothing visible or in flight."""
    def drained():
        a = stack.queue_state()
        return a.get("ApproximateNumberOfMessages") == "0" and a.get("ApproximateNumberOfMessagesNotVisible") == "0"
    return wait_until(drained, timeout, 0.2)


LEGACY_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy_app")


class _CurrentPoolRelay:
    """The pre-switch worker's endpoint: relays to the emulator and, when armed for a request, kills the
    worker right after the call that deleted that request's review_compose item (its cleanup), before
    the worker sees the reply. That is the state a reclaim leaves behind just before the switch."""

    def __init__(self, stack):
        self.stack = stack
        self.proc = None
        self.cut_req = None
        self.cut = threading.Event()

    def relay(self, slot, method, path, headers, body):
        service, op = _op_and_service(headers, body)
        status, reason, resp_headers, data = forward(method, path, headers, body)
        if (self.cut_req and status == 200 and service == "dynamodb" and op == "DeleteItem"
                and _json(body).get("TableName") == self.stack.t_compose
                and _json(body).get("Key", {}).get("req_id", {}).get("S") == self.cut_req):
            self.cut_req = None
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.cut.set()
            return None
        return status, reason, resp_headers, data, None


def run_current_pool(stack, logdir, plan):
    """The pool as it runs before the switch: one worker on the current (shipped) code, fed one part at a
    time. `plan` is [(request, number of its parts to process, cut)]. With cut, the worker is reclaimed
    right after the completing upload's cleanup (the entry must be last); its message is left in flight
    for the new pool. Otherwise the worker is stopped between messages once its parts are done."""
    os.makedirs(logdir, exist_ok=True)
    relay = _CurrentPoolRelay(stack)
    server = _ProxyServer(("127.0.0.1", 0), _ProxyHandler)
    server.slot, server.controller = None, relay
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_") and not k.startswith("TABLE_")}
    env.update(stack.worker_env())
    env.update({"AWS_ENDPOINT_URL": f"http://127.0.0.1:{server.server_address[1]}", "AWS_DEFAULT_REGION": "us-east-1",
                "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "PYTHONPATH": LEGACY_APP,
                "PYTHONUNBUFFERED": "1"})
    log = open(os.path.join(logdir, "current-1.log"), "wb")
    proc = subprocess.Popen(
        ["python3", os.path.join(LEGACY_APP, "compose_worker.py"), "--worker-id", "current-1"],
        cwd=tempfile.mkdtemp(prefix="current-1-"), env=env, stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    relay.proc = proc
    log.close()
    try:
        for k, (req, n, cut) in enumerate(plan):
            if cut and (k != len(plan) - 1 or n != N_PARTS):
                raise RuntimeError("verifier: a cut request must be the last, with all its parts")
            if not req.order:
                req.order = random.sample(PARTS, N_PARTS)
            for i, part in enumerate(req.order[:n], start=1):
                if cut and i == N_PARTS:
                    relay.cut_req = req.req_id
                stack.send(req, part)
                if cut and i == N_PARTS:
                    if not relay.cut.wait(READY_TIMEOUT_S):
                        raise RuntimeError(f"verifier: the pre-switch pool never cleaned up {req.req_id}")
                elif not wait_until(lambda: stack.reply_exists(req.req_id, part), READY_TIMEOUT_S, 0.05):
                    raise RuntimeError(f"verifier: the pre-switch pool did not process {req.req_id} {part}")
            req.notes.append(f"first {n} part(s) processed by the pre-switch pool"
                             + (", reclaimed right after the completing upload's cleanup" if cut else ""))
        if not relay.cut.is_set() and not wait_drained(stack, READY_TIMEOUT_S):
            raise RuntimeError("verifier: the pre-switch pool did not drain its messages")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        server.shutdown()
        server.server_close()


def wait_complete(stack, reqs, deadline):
    pending = list(reqs)
    while pending and time.time() < deadline:
        pending = [r for r in pending if not stack.is_complete(r)]
        if pending:
            time.sleep(0.25)
    return pending


def check_request(stack, req, snap, expect_ranks):
    """Problems with one request's graded state; empty when it is right."""
    p = []
    rid = req.req_id
    if snap["compose_item"] is not None:
        p.append(f"{rid}: review_compose item still present after the request finished: {snap['compose_item']}")
    if not snap["handed_off"]:
        got = [h for h in stack.handoffs if h["body"].get("review_id") == req.review_id]
        p.append(f"{rid}: the user-review service never accepted review {req.review_id} "
                 f"({len(got)} message(s) for it, none with the review stored and its values)")
    replies = snap["replies"]
    missing = [part for part in PARTS if part not in replies]
    if missing:
        p.append(f"{rid}: no reply for {missing}")
    if expect_ranks:
        for i, part in enumerate(req.order, start=1):
            if part in replies:
                rank, composed = replies[part]
                if rank != i:
                    p.append(f"{rid}: part {part} was #{i} to arrive but replied rank {rank!r}")
                if composed is not (i == N_PARTS):
                    p.append(f"{rid}: part {part} (#{i} to arrive) replied composed={composed!r}")
    if not missing:
        ranks = sorted(r for r, _ in replies.values() if isinstance(r, int))
        if ranks != list(range(1, N_PARTS + 1)):
            p.append(f"{rid}: reply ranks are {sorted(map(repr, (r for r, _ in replies.values())))}, expected 1..5 once each")
        composed = [part for part, (_, c) in replies.items() if c is True]
        if len(composed) != 1:
            p.append(f"{rid}: {len(composed)} replies say composed=true ({composed})")
        for part, (rank, c) in replies.items():
            if c not in (True, False):
                p.append(f"{rid}: reply {part} has composed={c!r}, not a boolean")
            elif c is True and rank != N_PARTS:
                p.append(f"{rid}: reply {part} says composed=true with rank {rank!r}")
    review = snap["review"]
    if review is None:
        p.append(f"{rid}: review {req.review_id} is not in reviews_store")
    else:
        for part in PARTS:
            if review.get(part) != req.values[part]:
                p.append(f"{rid}: review's {part} is {review.get(part)!r}, expected {req.values[part]!r}")
        carriers = {r.req_id for r in stack.requests if r.review_id == req.review_id}
        stored_by = (stack.stored_review(req.review_id) or {}).get("req_id", {}).get("S")
        if stored_by not in carriers:
            p.append(f"{rid}: review {req.review_id} stored with req_id {stored_by!r}, not a request that carried it")
    if req.review_id in stack.taken_down:
        if snap["on_movie"]:
            p.append(f"{rid}: review {req.review_id} was taken down by moderation but is back on movie "
                     f"{req.movie_id}'s page ({snap['on_movie']} time(s))")
    elif snap["on_movie"] != 1:
        p.append(f"{rid}: review {req.review_id} appears {snap['on_movie']} times in movie {req.movie_id}'s review_ids")
    return p


def check_movies(stack):
    p = []
    expected = {}
    for req in stack.requests:
        expected.setdefault(req.movie_id, set()).add(req.review_id)
    for movie, ids in expected.items():
        listed = stack.review_ids(movie)
        dup = [k for k, n in Counter(listed).items() if n > 1]
        if dup:
            p.append(f"movie {movie}: review ids listed more than once: {dup}")
        foreign = sorted(set(listed) - ids)
        if foreign:
            p.append(f"movie {movie}: unexpected review ids {foreign}")
    return p


def check_reviews(stack):
    """reviews_store holds the submitted reviews and nothing else."""
    known = {r.review_id for r in stack.requests}
    foreign = sorted(i.get("review_id", {}).get("S", repr(i)) for i in stack.all_reviews()
                     if i.get("review_id", {}).get("S") not in known)
    return [f"reviews_store holds items for unknown review ids {foreign}"] if foreign else []


def check_handoffs(stack):
    """Every message the user-review service saw must describe a submitted review truthfully."""
    p = []
    by_id = {r.review_id: r for r in stack.requests}
    for h in stack.handoffs:
        body = h["body"]
        req = by_id.get(body.get("review_id"))
        if req is None:
            p.append(f"user-review message for an unknown review: {body}")
            continue
        want = {"user_id": req.values["user_id"], "movie_id": req.movie_id, "rating": req.values["rating"]}
        wrong = {k: body.get(k) for k, v in want.items() if body.get(k) != v}
        if wrong:
            p.append(f"{req.req_id}: user-review message for {req.review_id} carries {wrong}, expected "
                     f"{ {k: want[k] for k in wrong} }")
    return p


def check_queue(stack):
    p = []
    attrs = stack.queue_state()
    if attrs.get("VisibilityTimeout") != str(VISIBILITY_TIMEOUT_S):
        p.append(f"queue VisibilityTimeout is {attrs.get('VisibilityTimeout')}, expected {VISIBILITY_TIMEOUT_S}")
    if attrs.get("RedrivePolicy"):
        p.append(f"queue has a RedrivePolicy: {attrs['RedrivePolicy']}")
    for k in ("ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"):
        if attrs.get(k, "0") != "0":
            p.append(f"queue {k} = {attrs.get(k)} after quiesce, expected 0")
    return p


def settle_and_grade(stack, pool, reqs, expect_ranks, snaps):
    """After every request has been snapshotted at its deadline: quiesce, re-read, compare."""
    problems = []
    for req in reqs:
        problems += check_request(stack, req, snaps[req.req_id], expect_ranks)
    time.sleep(QUIESCE_S)
    for req in reqs:
        later = stack.snapshot(req)
        if later != snaps[req.req_id]:
            problems.append(f"{req.req_id}: state changed after the deadline: {snaps[req.req_id]} -> {later}")
    problems += check_movies(stack)
    problems += check_reviews(stack)
    problems += check_handoffs(stack)
    problems += check_queue(stack)
    report = {
        "requests": [{"req_id": r.req_id, "order": r.order, "values": r.values, "notes": r.notes,
                      "snapshot": snaps[r.req_id]} for r in reqs],
        "kills": pool.kill_count(),
        "user_review_messages": len(stack.handoffs),
        "problems": problems,
    }
    with open(os.path.join(pool.logdir, "report.json"), "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    return problems
