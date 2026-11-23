"""Verifier harness: two replicas of the program under test, each behind its own fault-proxy
route, a janitor-style delete client, and an auditor that reads the bucket directly."""

import concurrent.futures
import hashlib
import http.client
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from faultproxy import FaultProxy, Rule, free_port

APP = os.environ.get("QS_VERIFY_APP", "/app")
FLOCI = os.environ.get("QS_VERIFY_ENDPOINT", "http://localhost:4566")
LOGS = os.environ.get("QS_VERIFY_LOGS", "/logs/verifier")
PYTHON = os.environ.get("QS_VERIFY_PYTHON", sys.executable)

ROOT = "docker/registry/v2"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TAG_MUTATION = r"/_manifests/tags/"
REVISION_MUTATION = r"/_manifests/revisions/"

# The janitor's policy (README): 30 s per attempt, retry every 2 s for up to 60 s.
JANITOR_TIMEOUT = 30
JANITOR_INTERVAL = 2
JANITOR_BUDGET = 60
QUIESCE = 1.5

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=32)

# Requests any replica sent somewhere other than the bucket, across the whole session.
VIOLATIONS = []


def s3_client():
    return boto3.client("s3", endpoint_url=FLOCI, region_name="us-east-1",
                        aws_access_key_id="test", aws_secret_access_key="test",
                        config=Config(s3={"addressing_style": "path"},
                                      retries={"max_attempts": 5, "mode": "standard"},
                                      max_pool_connections=64))


def manifest(label):
    """A distinct OCI image manifest."""
    cfg = hashlib.sha256(("config:" + label).encode()).hexdigest()
    layer = hashlib.sha256(("layer:" + label).encode()).hexdigest()
    doc = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                   "digest": "sha256:" + cfg, "size": 1234},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": "sha256:" + layer, "size": 4321}],
        "annotations": {"org.opencontainers.image.ref.name": label},
    }
    body = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()
    return body, "sha256:" + hashlib.sha256(body).hexdigest()


def hex_of(digest):
    return digest.split(":", 1)[1]


def rev_key(repo, digest):
    return f"{ROOT}/repositories/{repo}/_manifests/revisions/sha256/{hex_of(digest)}/link"


def blob_key(digest):
    h = hex_of(digest)
    return f"{ROOT}/blobs/sha256/{h[:2]}/{h}/data"


def tags_prefix(repo):
    return f"{ROOT}/repositories/{repo}/_manifests/tags/"


class Result:
    def __init__(self, status, headers=None, body=b"", t=None):
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.t = t or time.time()

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def __repr__(self):
        return f"<{self.status} {self.body[:200]!r}>"


def http_call(port, method, path, body=None, headers=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return Result(resp.status, dict(resp.getheaders()), data)
    except (socket.timeout, TimeoutError) as exc:
        return Result("timeout", body=str(exc).encode())
    except (ConnectionError, http.client.HTTPException, OSError) as exc:
        return Result("error", body=repr(exc).encode())
    finally:
        conn.close()


class Auditor:
    """Reads the registry layout straight from the bucket."""

    def __init__(self, bucket):
        self.bucket = bucket
        self.s3 = s3_client()

    def body(self, key):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode(
                errors="replace").strip()
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def keys(self, prefix):
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            out.extend(o["Key"] for o in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                return out
            token = resp["NextContinuationToken"]

    def live(self, repo, digest):
        return self.body(rev_key(repo, digest)) == digest and self.body(blob_key(digest)) is not None

    def current(self, repo):
        """{tag: body of current/link} for every tag directory that has one."""
        out = {}
        prefix = tags_prefix(repo)
        for key in self.keys(prefix):
            rest = key[len(prefix):]
            tag, _, tail = rest.partition("/")
            if tail == "current/link":
                out[tag] = self.body(key)
        return {t: b for t, b in out.items() if b is not None}

    def snapshot(self, repo):
        cur = self.current(repo)
        digests = {b for b in cur.values() if DIGEST_RE.match(b or "")}
        live = {d: self.live(repo, d) for d in digests}
        return cur, live

    def broken(self, repo):
        """Tags whose current/link names a digest with no live revision (the audit's rule)."""
        cur, live = self.snapshot(repo)
        return sorted(t for t, b in cur.items() if DIGEST_RE.match(b) and not live[b])

    def resolving(self, repo):
        cur, live = self.snapshot(repo)
        return sorted(t for t, b in cur.items() if DIGEST_RE.match(b) and live[b])

    def seed(self, repo, content, digest, tags):
        """Write a manifest and its tags in the layout earlier versions wrote."""
        self.s3.put_object(Bucket=self.bucket, Key=blob_key(digest), Body=content)
        self.s3.put_object(Bucket=self.bucket, Key=rev_key(repo, digest), Body=digest.encode())

        def one(tag):
            base = tags_prefix(repo) + tag
            self.s3.put_object(Bucket=self.bucket,
                               Key=f"{base}/index/sha256/{hex_of(digest)}/link",
                               Body=digest.encode())
            self.s3.put_object(Bucket=self.bucket, Key=f"{base}/current/link",
                               Body=digest.encode())
        list(_pool.map(one, tags))


class Replica:
    def __init__(self, rid, endpoint, bucket, logdir):
        self.rid = rid
        self.endpoint = endpoint
        self.bucket = bucket
        self.port = free_port()
        self.logdir = logdir
        self.proc = None
        self.starts = 0

    def start(self, ready_timeout=20):
        self.starts += 1
        log = open(os.path.join(self.logdir, f"replica-{self.rid}-{self.starts}.log"), "wb")
        env = dict(os.environ)
        env.update({"PYTHONPATH": APP, "AWS_ENDPOINT_URL": self.endpoint,
                    "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                    "AWS_DEFAULT_REGION": "us-east-1", "PYTHONDONTWRITEBYTECODE": "1"})
        for k in list(env):
            if k.startswith("AWS_ENDPOINT_URL_"):
                env.pop(k)
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "quayside", "--listen", f"127.0.0.1:{self.port}",
             "--s3-endpoint", self.endpoint, "--bucket", self.bucket, "--replica-id", self.rid],
            cwd=tempfile.mkdtemp(prefix=f"replica-{self.rid}-"), env=env,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"replica {self.rid} exited with {self.proc.returncode} "
                                     f"before serving /healthz")
            r = http_call(self.port, "GET", "/healthz", timeout=2)
            if r.status == 200:
                return
            time.sleep(0.2)
        raise AssertionError(f"replica {self.rid} did not answer /healthz within {ready_timeout}s")

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(10)

    def restart(self):
        self.kill()
        self.start()

    # -- API -------------------------------------------------------------------------------
    def push(self, repo, ref, content, timeout=30):
        return http_call(self.port, "PUT", f"/v2/{repo}/manifests/{ref}", body=content,
                         headers={"Content-Type": "application/vnd.oci.image.manifest.v1+json"},
                         timeout=timeout)

    def get(self, repo, ref, timeout=30):
        return http_call(self.port, "GET", f"/v2/{repo}/manifests/{ref}", timeout=timeout)

    def delete(self, repo, ref, timeout=JANITOR_TIMEOUT):
        return http_call(self.port, "DELETE", f"/v2/{repo}/manifests/{ref}", timeout=timeout)

    def tags_list(self, repo, timeout=30):
        return http_call(self.port, "GET", f"/v2/{repo}/tags/list", timeout=timeout)

    def async_(self, fn, *args, **kw):
        return _pool.submit(fn, *args, **kw)


class Cluster:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.logdir = os.path.join(LOGS, "cases", re.sub(r"[^A-Za-z0-9_.-]+", "_", name))
        os.makedirs(self.logdir, exist_ok=True)
        host, port = FLOCI.split("//", 1)[1].split(":")
        self.proxy = FaultProxy(host, int(port), bucket)
        self.a = Replica("a", self.proxy.start_route("a"), bucket, self.logdir)
        self.b = Replica("b", self.proxy.start_route("b"), bucket, self.logdir)
        self.audit = Auditor(bucket)
        self.notes = []
        self.strict_202 = None
        self.problems_202 = []

    def start(self):
        self.a.start()
        self.b.start()
        return self

    def stop(self):
        for r in (self.a, self.b):
            r.kill()
        self.proxy.stop()
        VIOLATIONS.extend(self.proxy.violations)
        try:
            with open(os.path.join(self.logdir, "s3-requests.jsonl"), "w") as fh:
                for e in self.proxy.log:
                    fh.write(json.dumps(e, default=str) + "\n")
            with open(os.path.join(self.logdir, "notes.txt"), "w") as fh:
                fh.write("\n".join(self.notes))
        except OSError:
            pass

    def note(self, msg):
        self.notes.append(f"{time.time():.3f} {msg}")

    def rule(self, route, action, methods=None, key_re=None, nth=1, last=None):
        return self.proxy.add(Rule(route, action, methods, key_re, nth, last))

    def new_repo(self, label):
        return f"verify/{label}-{secrets.token_hex(3)}"

    # -- janitor ---------------------------------------------------------------------------
    def janitor_delete(self, replica, repo, digest, first=None, budget=JANITOR_BUDGET):
        """Delete like the janitor: retry anything but 202/404 every 2 s for up to 60 s.
        first(result) runs after the first attempt returns, before any retry."""
        attempts = []
        t0 = time.time()
        while True:
            r = replica.delete(repo, digest)
            attempts.append(r)
            self.note(f"janitor DELETE {digest[:19]} via {replica.rid} -> {r.status}")
            if r.status == 202:
                self.on_202(repo, digest)
            if len(attempts) == 1 and first is not None:
                first(r)
            if r.status in (202, 404):
                return attempts
            if time.time() - t0 >= budget:
                return attempts
            time.sleep(JANITOR_INTERVAL)

    def on_202(self, repo, digest):
        """When strict_202 is set (no tag write of digest can be in flight), a 202 must mean
        the revision is gone and no tag names digest at the moment it is answered."""
        if not self.strict_202:
            return
        cur = self.audit.current(repo)
        naming = sorted(t for t, b in cur.items() if b == digest)
        if self.audit.live(repo, digest):
            self.problems_202.append(f"DELETE {digest} answered 202 while the revision was live")
        if naming:
            self.problems_202.append(f"DELETE {digest} answered 202 while {len(naming)} tags "
                                     f"still named it: {naming[:5]}")

    def seed_case(self, label, n_tags, extra=()):
        """A repository with manifest D tagged t000.. (+extra) and manifest X tagged 'other',
        written in the existing layout."""
        repo = self.new_repo(label)
        m, d = manifest(repo + ":D")
        mx, x = manifest(repo + ":X")
        seeded = [f"t{i:03d}" for i in range(n_tags)] + list(extra)
        self.audit.seed(repo, m, d, seeded)
        self.audit.seed(repo, mx, x, ["other"])
        return repo, (m, d), (mx, x), seeded


def wait_fired_or_done(rule, future, timeout):
    """Wait until rule fired or future finished; return whether the rule fired."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if rule.fired.is_set():
            return True
        if future.done():
            time.sleep(0.05)
            return rule.fired.is_set()
        time.sleep(0.02)
    return rule.fired.is_set()


def statuses(results):
    return [r.status for r in results]


def check_end_state(cl, repo, d, seeded, other=None, x=None, pushes=(), moved=None,
                    attempts=None):
    """Return a list of violated expectations after a case finished.

    pushes: [(tag, result)] pushes of digest d made during the case.
    moved: (tag, digest) a tag retargeted away from d during the case.
    """
    time.sleep(QUIESCE)
    problems = []
    au = cl.audit
    cur, live = au.snapshot(repo)
    d_live = au.live(repo, d)

    if attempts is not None:
        last = attempts[-1].status if attempts else None
        if last not in (202, 404):
            problems.append(f"janitor gave up on {d}: attempts answered {statuses(attempts)}")

    broken = sorted(t for t, b in cur.items() if DIGEST_RE.match(b) and not live[b])
    if broken:
        problems.append(f"tags naming a digest without a live revision: {broken[:10]}"
                        f"{' ...' if len(broken) > 10 else ''} ({len(broken)} total)")

    still = sorted(t for t in seeded if cur.get(t) == d)
    if still:
        problems.append(f"{len(still)} tags that pointed at {d} before the delete still name it: "
                        f"{still[:5]}")

    ok_pushes = [(t, r) for t, r in pushes if r.status == 201]
    if d_live and not pushes:
        problems.append(f"{d} is still live after the delete was acknowledged")
    if d_live:
        for t, r in ok_pushes:
            if cur.get(t) != d:
                problems.append(f"push of {t} -> {d} answered 201 and {d} is live, but {t} "
                                f"resolves to {cur.get(t)!r}")

    if other is not None:
        if cur.get(other) != x or not au.live(repo, x):
            problems.append(f"tag {other} should still resolve to {x}: current/link="
                            f"{cur.get(other)!r}, live={au.live(repo, x)}")
    if moved is not None:
        mt, md = moved
        if cur.get(mt) != md:
            problems.append(f"tag {mt} was retargeted to {md} during the delete, now "
                            f"{cur.get(mt)!r}")

    expected = sorted(t for t, b in cur.items() if DIGEST_RE.match(b) and live[b])
    for rep in (cl.a, cl.b):
        r = rep.tags_list(repo)
        listed = (r.json() or {}).get("tags") if r.status == 200 else None
        if r.status == 404 and not expected:
            continue
        if listed is None or sorted(listed) != expected:
            problems.append(f"tags/list on replica {rep.rid} answered {r.status} "
                            f"{sorted(listed) if listed is not None else r.body[:200]!r}; "
                            f"tags that resolve: {expected}")

    g = cl.a.get(repo, d)
    if d_live and g.status != 200:
        problems.append(f"GET by digest of live {d} answered {g.status}")
    if not d_live and g.status != 404:
        problems.append(f"GET by digest of deleted {d} answered {g.status}, expected 404")
    return problems
