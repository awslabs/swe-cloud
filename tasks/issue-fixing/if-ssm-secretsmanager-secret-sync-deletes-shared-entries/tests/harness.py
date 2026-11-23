"""Verifier harness: a fault-injecting HTTP proxy in front of Floci, a fresh-pod runner for
syncctl invocations, and readers of SSM / Secrets Manager state.

Nothing here imports solver code. Every graded fact is read from the emulator directly.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 4566
ENDPOINT = f"http://localhost:{UPSTREAM_PORT}"
APP_DIR = os.environ.get("VERIFIER_APP_DIR", "/app")
SYNCCTL = os.path.join(APP_DIR, "bin", "syncctl")
INVOCATION_TIMEOUT_S = 40

VERIFIER_BASE = tempfile.mkdtemp(prefix="syncctl-verifier-")

# Local disk a run might use to remember things between runs. Every run is a fresh pod
# (docs/OPERATIONS.md), so whatever a run leaves here is wiped before the next one.
SWEEP_ROOTS = os.environ.get(
    "VERIFIER_SWEEP_ROOTS", "/app:/root:/home:/tmp:/var/tmp:/var/lib:/var/cache:/srv:/opt:/mnt").split(":")
SWEEP_EXCLUDE = ["/opt/floci", "/tmp/vertx-cache", "/tmp/floci.log", "/var/lib/dpkg", "/var/lib/apt",
                 "/var/cache/apt", "/var/cache/debconf", "/var/cache/ldconfig", VERIFIER_BASE]


def aws_env() -> dict:
    return {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}


def direct_client(service: str):
    return boto3.client(service, endpoint_url=ENDPOINT, region_name="us-east-1",
                        aws_access_key_id="test", aws_secret_access_key="test",
                        config=Config(retries={"max_attempts": 5, "mode": "standard"}))


def floci_live() -> bool:
    try:
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=5)
        conn.request("GET", "/_floci/health")
        ok = conn.getresponse().status == 200
        conn.close()
        return ok
    except OSError:
        return False


# --------------------------------------------------------------------------- fault proxy

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _handle(self):
        proxy: FaultProxy = self.server.fault_proxy  # type: ignore[attr-defined]
        body = self._read_body()
        target = self.headers.get("X-Amz-Target", "")
        if proxy.dead():
            self.close_connection = True
            return
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("connection", "keep-alive", "proxy-connection", "transfer-encoding",
                                        "content-length")}
        headers["Content-Length"] = str(len(body))
        proxy.before_forward(target, body)
        upstream = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=60)
        try:
            upstream.request(self.command, self.path, body=body, headers=headers)
            resp = upstream.getresponse()
            data = resp.read()
            status, resp_headers = resp.status, resp.getheaders()
        finally:
            upstream.close()

        if proxy.observe(target, body, status, data):
            # Floci has applied the call; the worker never hears about it (killed, or the reply is lost).
            self.close_connection = True
            return

        self.send_response_only(status, resp.reason)
        for k, v in resp_headers:
            if k.lower() not in ("transfer-encoding", "content-length", "connection", "keep-alive"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _handle


# Writes whose first reply the proxy can lose after Floci applied them. UpdateSecret is left out:
# Floci rejects a same-token retry of it that AWS documents as ignored.
LOSSY_TARGETS = {
    "AmazonSSM.PutParameter", "AmazonSSM.DeleteParameter", "AmazonSSM.DeleteParameters",
    "AmazonSSM.AddTagsToResource", "AmazonSSM.RemoveTagsFromResource",
    "secretsmanager.CreateSecret", "secretsmanager.PutSecretValue", "secretsmanager.DeleteSecret",
    "secretsmanager.RestoreSecret", "secretsmanager.TagResource", "secretsmanager.UntagResource",
}


def _subject(req: dict) -> str:
    for key in ("Name", "SecretId", "ResourceId"):
        if req.get(key):
            return str(req[key])
    return ",".join(sorted(req.get("Names") or []))


class FaultProxy:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.fault_proxy = self  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.lock = threading.Lock()
        self.pgid_ready = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.arm(None)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def arm(self, kill_after: int | None, lose_replies: bool = False, before_create=None, after_delete=None):
        """``lose_replies``: drop the first successful reply of each write (per call and entry), the
        way a reset connection does after AWS applied the call; the SDK retries it. ``before_create``:
        {name: action} run once, just before the first PutParameter for ``name`` is forwarded.
        ``after_delete``: {name: action} run once, right after Floci applied a delete of ``name`` and
        before the worker hears anything back."""
        with self.lock:
            self.lose_replies = lose_replies
            self.before_create = dict(before_create or {})
            self.after_delete = dict(after_delete or {})
            self.lost: list[str] = []
            self.lost_keys: set = set()
            self.raced: list[str] = []
            self.taken_over: list[str] = []
            self.kill_after = kill_after
            self.pgid = None
            self.killed = False
            self.requests: list[str] = []
            self.creates: list[str] = []
            self.pgid_ready.clear()

    def set_pgid(self, pgid: int):
        with self.lock:
            self.pgid = pgid
        self.pgid_ready.set()

    def dead(self) -> bool:
        with self.lock:
            return self.killed

    def before_forward(self, target: str, body: bytes):
        if target != "AmazonSSM.PutParameter":
            return
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return
        with self.lock:
            action = self.before_create.pop(req.get("Name"), None)
            if action is not None:
                self.raced.append(req.get("Name"))
        if action is not None:
            action()

    def _deleted_names(self, target: str, req: dict, resp: dict) -> list[str]:
        if target == "AmazonSSM.DeleteParameters":
            return list(resp.get("DeletedParameters") or [])
        if target == "AmazonSSM.DeleteParameter":
            return [req.get("Name")]
        if target == "secretsmanager.DeleteSecret":
            return [resp.get("Name")]
        return []

    def observe(self, target: str, body: bytes, status: int, data: bytes) -> bool:
        """Record one forwarded request; return True if the worker gets no reply (killed, or lost)."""
        if self.after_delete and 200 <= status < 300:
            try:
                req, resp = json.loads(body or b"{}"), json.loads(data or b"{}")
            except ValueError:
                req, resp = {}, {}
            for name in self._deleted_names(target, req, resp):
                with self.lock:
                    action = self.after_delete.pop(name, None)
                if action is not None and action():
                    with self.lock:
                        self.taken_over.append(name)
        if self.lose_replies and 200 <= status < 300 and target in LOSSY_TARGETS:
            try:
                key = (target, _subject(json.loads(body or b"{}")))
            except ValueError:
                key = (target, "")
            with self.lock:
                if key not in self.lost_keys:
                    self.lost_keys.add(key)
                    self.requests.append(target)
                    self.lost.append(f"{target.split('.')[-1]}({key[1]})")
                    return True
        created = None
        if status == 200 and target in ("AmazonSSM.PutParameter", "secretsmanager.CreateSecret"):
            try:
                req, resp = json.loads(body or b"{}"), json.loads(data or b"{}")
            except ValueError:
                req, resp = {}, {}
            if target == "secretsmanager.CreateSecret" or resp.get("Version") == 1:
                created = req.get("Name")
        with self.lock:
            self.requests.append(target or "<no target>")
            if created is None:
                return False
            self.creates.append(created)
            if self.kill_after is None or self.killed or len(self.creates) != self.kill_after:
                return False
            self.killed = True
        self.pgid_ready.wait(10)
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except (ProcessLookupError, TypeError):
            pass
        return True


# --------------------------------------------------------------------------- runner

@dataclass
class RunResult:
    label: str
    command: str
    returncode: int | None
    stdout: str
    stderr: str
    requests: list[str] = field(default_factory=list)
    creates: list[str] = field(default_factory=list)
    killed: bool = False
    timed_out: bool = False
    swept: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    raced: list[str] = field(default_factory=list)
    taken_over: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (f"[{self.label}] syncctl {self.command}: rc={self.returncode} killed={self.killed} "
                f"timed_out={self.timed_out} requests={len(self.requests)} creates={self.creates}\n"
                f"replies lost (the SDK retried): {self.lost}\n"
                f"names created by hand while the run was creating them: {self.raced}\n"
                f"names re-created by hand right after the run deleted them: {self.taken_over}\n"
                f"stdout: {self.stdout.strip()[-1500:]}\nstderr: {self.stderr.strip()[-3000:]}")


def _inventory() -> dict[str, float]:
    files: dict[str, float] = {}
    for root in SWEEP_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if any(dirpath == ex or dirpath.startswith(ex + "/") for ex in SWEEP_EXCLUDE):
                dirnames[:] = []
                continue
            for fn in filenames:
                path = os.path.join(dirpath, fn)
                if path in SWEEP_EXCLUDE:
                    continue
                try:
                    files[path] = os.lstat(path).st_mtime
                except OSError:
                    pass
    return files


def _sweep(before: dict[str, float]) -> list[str]:
    """Delete what the run left on local disk: every new file, and every pre-existing file it
    modified outside /tmp."""
    removed = []
    for path, mtime in _inventory().items():
        if path in before and (before[path] == mtime or path.startswith("/tmp/")):
            continue
        try:
            os.unlink(path)
            removed.append(path)
        except OSError:
            pass
    return removed


class Runner:
    def __init__(self, proxy: FaultProxy):
        self.proxy = proxy
        self.configs = tempfile.mkdtemp(prefix="configs-", dir=VERIFIER_BASE)

    def write_sync(self, name: str, config: dict, source: dict) -> str:
        src = os.path.join(self.configs, f"{name}.source.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump(source, fh)
        cfg = dict(config, source_file=src)
        path = os.path.join(self.configs, f"{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return path

    def run(self, label: str, command: str, config_path: str, kill_after: int | None = None,
            lose_replies: bool = False, before_create=None, after_delete=None) -> RunResult:
        pod = tempfile.mkdtemp(prefix="pod-", dir=VERIFIER_BASE)
        for sub in ("work", "home", "tmp"):
            os.mkdir(os.path.join(pod, sub))
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "HOME": os.path.join(pod, "home"),
            "TMPDIR": os.path.join(pod, "tmp"),
            "PYTHONPATH": APP_DIR,
            "AWS_ENDPOINT_URL": self.proxy.endpoint,
            **aws_env(),
        }
        before = _inventory()
        self.proxy.arm(kill_after, lose_replies, before_create, after_delete)
        proc = subprocess.Popen([SYNCCTL, command, "--config", config_path], cwd=os.path.join(pod, "work"),
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        self.proxy.set_pgid(proc.pid)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=INVOCATION_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
        # Anything else the run's process group left running dies with the pod.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        with self.proxy.lock:
            requests, creates, killed = list(self.proxy.requests), list(self.proxy.creates), self.proxy.killed
            lost, raced, taken = list(self.proxy.lost), list(self.proxy.raced), list(self.proxy.taken_over)
        self.proxy.arm(None)
        shutil.rmtree(pod, ignore_errors=True)
        swept = _sweep(before)
        return RunResult(label, command, proc.returncode, out or "", err or "", requests, creates, killed,
                         timed_out, swept, lost, raced, taken)


# --------------------------------------------------------------------------- state readers

def _ssm_tags(ssm, name: str) -> dict:
    resp = ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)
    return {t["Key"]: t.get("Value", "") for t in resp.get("TagList", [])}


def ssm_record(ssm, name: str) -> dict:
    p = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]
    return {"value": p.get("Value"), "type": p.get("Type"), "version": p.get("Version"), "tags": _ssm_tags(ssm, name)}


def sm_record(sm, name: str, deleted: bool = False) -> dict:
    desc = sm.describe_secret(SecretId=name)
    rec = {"deleted": deleted or bool(desc.get("DeletedDate")),
           "tags": {t["Key"]: t.get("Value", "") for t in desc.get("Tags", [])},
           "versions": {k: sorted(v) for k, v in (desc.get("VersionIdsToStages") or {}).items()},
           "value": None}
    if not rec["deleted"]:
        try:
            rec["value"] = sm.get_secret_value(SecretId=name).get("SecretString")
        except ClientError:
            rec["value"] = None
    return rec


def ssm_snapshot() -> dict[str, dict]:
    """Every customer parameter (public /aws/ namespace excluded): value, type, version, tags."""
    ssm = direct_client("ssm")
    out: dict[str, dict] = {}
    kwargs = {"Path": "/", "Recursive": True, "WithDecryption": True}
    while True:
        page = ssm.get_parameters_by_path(**kwargs)
        for p in page.get("Parameters", []):
            if p["Name"].startswith("/aws/"):
                continue
            out[p["Name"]] = {"value": p.get("Value"), "type": p.get("Type"), "version": p.get("Version")}
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]
    for name, rec in out.items():
        rec["tags"] = _ssm_tags(ssm, name)
    return out


def sm_snapshot() -> dict[str, dict]:
    """Every secret, including ones pending deletion: value, tags, version stages, deleted flag."""
    sm = direct_client("secretsmanager")
    out: dict[str, dict] = {}
    kwargs: dict = {"IncludePlannedDeletion": True, "MaxResults": 100}
    while True:
        page = sm.list_secrets(**kwargs)
        for s in page.get("SecretList", []):
            out[s["Name"]] = {"deleted": bool(s.get("DeletedDate"))}
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]
    return {name: sm_record(sm, name, rec["deleted"]) for name, rec in out.items()}


def reset_ssm():
    ssm = direct_client("ssm")
    names = list(ssm_snapshot())
    for i in range(0, len(names), 10):
        ssm.delete_parameters(Names=names[i: i + 10])


def reset_sm():
    sm = direct_client("secretsmanager")
    for name, rec in sm_snapshot().items():
        if rec["deleted"]:
            try:
                sm.restore_secret(SecretId=name)
            except ClientError:
                pass
        try:
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        except ClientError:
            pass
