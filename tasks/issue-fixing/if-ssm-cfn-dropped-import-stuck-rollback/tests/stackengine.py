"""Local stand-in for the CloudFormation stack engine.

Implements the part of CloudFormation's stack and custom-resource behaviour our stacks rely on;
docs/stack-engine.md is the contract.  Every resource operation runs as its own short-lived
process (see ``Sandbox``), one at a time, and the engine waits for its answer before sending the
next request.
"""
import copy
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import boto3
from botocore.exceptions import ClientError

HANDLER_TIMEOUT_SECONDS = 30
CLEANUP_DELETE_ATTEMPTS = 3
HANDLER_USER = "cfnhandler"
ACCOUNT_ID = "000000000000"
DEFAULT_TABLE = "cdk-crossref-tags"

FUNCTION_TYPE = "Local::Function"

DYNAMIC_REF = re.compile(r"\{\{resolve:ssm:([A-Za-z0-9_.\-/]+)\}\}")

# directories any local user can write to; nothing a handler leaves there survives it
_SHARED_WRITABLE = ["/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/opt/floci/data"]

DEPLOYABLE = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}


class ResolveError(Exception):
    pass


def aws_env():
    return {
        "AWS_ENDPOINT_URL": os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
        "AWS_REGION": os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")),
        "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    }


class Sandbox:
    """Runs one resource operation as a process and makes sure nothing of it outlives the answer.

    The process gets a fresh empty working directory and HOME, its own process group, and (when the
    engine runs as root) the unprivileged ``cfnhandler`` user.  As soon as the response line has been
    read (or the process exits, or the timeout expires) the whole group is killed; any other process
    still running as the handler user is killed too and reported, and files the handler user left in
    shared writable directories are removed.
    """

    def __init__(self, app_dir="/app", table=DEFAULT_TABLE, timeout=HANDLER_TIMEOUT_SECONDS, log_dir=None):
        self.app_dir = app_dir
        self.table = table
        self.timeout = timeout
        self.log_dir = log_dir
        self.uid = self.gid = None
        if os.geteuid() == 0:
            try:
                entry = pwd.getpwnam(HANDLER_USER)
                self.uid, self.gid = entry.pw_uid, entry.pw_gid
            except KeyError:
                pass

    def _env(self, workdir, marker, extra, pythonpath):
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "HOME": workdir,
            "TMPDIR": workdir,
            "PYTHONPATH": pythonpath or self.app_dir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "CROSSREF_TABLE": self.table,
            "STACKCTL_INVOCATION": marker,
        }
        env.update(aws_env())
        env.update(extra or {})
        return env

    def run(self, argv, stdin_text="", extra_env=None, want_response=True, label="op", pythonpath=None):
        marker = uuid.uuid4().hex
        workdir = tempfile.mkdtemp(prefix="stackctl-op-")
        kwargs = {}
        if self.uid is not None:
            os.chown(workdir, self.uid, self.gid)
            kwargs.update(user=self.uid, group=self.gid, extra_groups=[])
        started = time.time()
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=workdir, env=self._env(workdir, marker, extra_env, pythonpath), start_new_session=True, **kwargs)
        lines, errs = [], []

        def read_out():
            try:
                lines.append(proc.stdout.readline() if want_response else proc.stdout.read())
            except Exception:  # pragma: no cover - pipe torn down by the kill
                pass

        def read_err():
            try:
                errs.append(proc.stderr.read())
            except Exception:  # pragma: no cover
                pass

        t_out = threading.Thread(target=read_out, daemon=True)
        t_err = threading.Thread(target=read_err, daemon=True)
        t_out.start()
        t_err.start()
        try:
            proc.stdin.write(stdin_text.encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass
        t_out.join(self.timeout)
        timed_out = t_out.is_alive()
        if not want_response and not timed_out:
            try:
                proc.wait(max(0.1, self.timeout - (time.time() - started)))
            except subprocess.TimeoutExpired:
                timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass
        strays = self._kill_strays(marker)
        t_err.join(1)
        swept = self._sweep_files()
        shutil.rmtree(workdir, ignore_errors=True)
        stdout = lines[0].decode(errors="replace") if lines and lines[0] else ""
        stderr = errs[0].decode(errors="replace") if errs and errs[0] else ""
        if self.log_dir:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(os.path.join(self.log_dir, f"{int(started * 1000)}-{label}.log"), "w") as fh:
                fh.write(f"$ {' '.join(argv)}\n--- stdin\n{stdin_text}\n--- stdout\n{stdout}\n--- stderr\n{stderr}\n")
        return {
            "stdout": stdout, "stderr": stderr, "returncode": proc.returncode,
            "timed_out": timed_out, "strays": strays, "swept_files": swept,
            "seconds": round(time.time() - started, 3),
        }

    def _kill_strays(self, marker):
        strays = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == os.getpid():
                continue
            pid = int(entry)
            try:
                if self.uid is not None:
                    with open(f"/proc/{pid}/status") as fh:
                        uid_line = next(l for l in fh if l.startswith("Uid:"))
                    if int(uid_line.split()[1]) != self.uid:
                        continue
                else:
                    with open(f"/proc/{pid}/environ", "rb") as fh:
                        if f"STACKCTL_INVOCATION={marker}".encode() not in fh.read():
                            continue
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmd = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
                os.kill(pid, signal.SIGKILL)
                strays.append({"pid": pid, "cmdline": cmd})
            except (OSError, StopIteration, ValueError):
                continue
        return strays

    def _sweep_files(self):
        if self.uid is None:
            return []
        swept = []
        for base in _SHARED_WRITABLE:
            if not os.path.isdir(base):
                continue
            for root, dirs, files in os.walk(base, topdown=True):
                for name in list(dirs) + files:
                    path = os.path.join(root, name)
                    try:
                        if os.lstat(path).st_uid == self.uid:
                            if os.path.isdir(path) and not os.path.islink(path):
                                shutil.rmtree(path, ignore_errors=True)
                                if name in dirs:
                                    dirs.remove(name)
                            else:
                                os.unlink(path)
                            swept.append(path)
                    except OSError:
                        continue
        return swept


class Engine:
    """Stacks, their committed resources, and the operations that move them."""

    def __init__(self, stacks=None, sandbox=None, observer=None, region=None, source_dir=None, asset_dir=None):
        self.stacks = stacks if stacks is not None else {}
        self.sandbox = sandbox or Sandbox()
        self.observer = observer
        self.region = region or aws_env()["AWS_REGION"]
        self.ssm = boto3.client("ssm", region_name=self.region)
        self.journal = []
        # the forward pass currently being applied: {"stack": name, "applied": {logical: record}}
        self.inflight = None
        # where {"Asset": <dir>} is read from, and where published assets are kept
        self.source_dir = source_dir or self.sandbox.app_dir
        self.asset_dir = asset_dir or os.path.join(os.environ.get("STACKCTL_HOME", "/var/lib/stackctl"), "assets")
        # the code each deployed function runs right now: {arn: {"handler": module, "code": dir}}
        self.functions = {}
        for stack in self.stacks.values():
            for rec in stack["resources"].values():
                if rec["type"] == FUNCTION_TYPE:
                    self._set_function(rec["physical_id"], rec["props"])

    # ------------------------------------------------------------------ journal

    def emit(self, kind, **fields):
        event = {"seq": len(self.journal) + 1, "time": round(time.time(), 3), "kind": kind, **fields}
        self.journal.append(event)
        if self.observer:
            self.observer(self, event)
        return event

    def _status(self, stack, status, reason=""):
        stack["status"] = status
        stack["status_reason"] = reason
        self.emit("status", stack=stack["name"], status=status, reason=reason)

    # ------------------------------------------------------------------ assets and functions

    def publish(self, template):
        """Replace every ``{"Asset": <dir>}`` by ``{"AssetHash": <hash>}`` after copying the directory
        (relative to ``source_dir``) into the asset store, as ``cdk deploy`` publishes assets first."""
        def walk(value):
            if isinstance(value, dict):
                if set(value) == {"Asset"}:
                    return {"AssetHash": self._publish_dir(value["Asset"])}
                return {k: walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v) for v in value]
            return value

        return walk(copy.deepcopy(template))

    def _publish_dir(self, rel):
        src = os.path.join(self.source_dir, rel)
        if not os.path.isdir(src):
            raise ValueError(f"asset directory {src} does not exist")
        digest = hashlib.sha256(rel.encode() + b"\0")
        for root, dirs, names in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(names):
                if name.endswith(".pyc"):
                    continue
                path = os.path.join(root, name)
                digest.update(os.path.relpath(path, src).encode() + b"\0")
                with open(path, "rb") as fh:
                    digest.update(hashlib.sha256(fh.read()).digest())
        asset = digest.hexdigest()[:20]
        dest = os.path.join(self.asset_dir, asset)
        if not os.path.isdir(dest):
            tmp = f"{dest}.tmp-{uuid.uuid4().hex[:8]}"
            shutil.copytree(src, os.path.join(tmp, rel), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for root, dirs, names in os.walk(tmp):
                for name in dirs + names:
                    path = os.path.join(root, name)
                    os.chmod(path, 0o755 if os.path.isdir(path) else 0o644)
            os.chmod(tmp, 0o755)
            os.replace(tmp, dest)
        return asset

    def function_arn(self, stack_name, logical):
        return f"arn:aws:lambda:{self.region}:{ACCOUNT_ID}:function:{stack_name}-{logical}"

    def _set_function(self, arn, props):
        self.functions[arn] = {"handler": props.get("Handler"),
                               "code": os.path.join(self.asset_dir, (props.get("Code") or {}).get("AssetHash", ""))}

    def _function(self, stack, request):
        arn = self.function_arn(stack["name"], request["LogicalResourceId"])
        if request["RequestType"] == "Delete":
            if request.get("PhysicalResourceId") == arn:
                self.functions.pop(arn, None)
            return {"Status": "SUCCESS", "PhysicalResourceId": request.get("PhysicalResourceId") or arn}
        props = request["ResourceProperties"]
        code = (props.get("Code") or {}).get("AssetHash")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", str(props.get("Handler", ""))):
            return {"Status": "FAILED", "Reason": f"invalid Handler {props.get('Handler')!r}"}
        if not code or not os.path.isdir(os.path.join(self.asset_dir, code)):
            return {"Status": "FAILED", "Reason": f"code asset {code!r} has not been published"}
        self._set_function(arn, props)
        return {"Status": "SUCCESS", "PhysicalResourceId": arn, "Data": {"Arn": arn}}

    # ------------------------------------------------------------------ resolution

    def resolve(self, props, stack_name=""):
        refs = {}

        def lookup(name):
            if name not in refs:
                try:
                    refs[name] = self.ssm.get_parameter(Name=name)["Parameter"]["Value"]
                except ClientError as exc:
                    if exc.response["Error"]["Code"] == "ParameterNotFound":
                        raise ResolveError(f"Parameters: [ssm:{name}] cannot be found.")
                    raise
            return refs[name]

        def walk(value):
            if isinstance(value, str):
                return DYNAMIC_REF.sub(lambda m: lookup(m.group(1)), value)
            if isinstance(value, list):
                return [walk(v) for v in value]
            if isinstance(value, dict):
                if set(value) == {"Fn::GetAtt"}:
                    logical, attribute = value["Fn::GetAtt"]
                    if attribute != "Arn":
                        raise ResolveError(f"Fn::GetAtt {logical}.{attribute}: only Arn is supported")
                    return self.function_arn(stack_name, logical)
                return {k: walk(v) for k, v in value.items()}
            return value

        return walk(copy.deepcopy(props)), refs

    # ------------------------------------------------------------------ resource operations

    def _invoke(self, stack, logical, rtype, request_type, props, physical=None, old_props=None):
        request = {
            "RequestType": request_type,
            "RequestId": str(uuid.uuid4()),
            "StackId": stack["id"],
            "LogicalResourceId": logical,
            "ResourceType": rtype,
            "ResourceProperties": props,
        }
        if physical is not None:
            request["PhysicalResourceId"] = physical
        if old_props is not None:
            request["OldResourceProperties"] = old_props
        self.emit("request", stack=stack["name"], logical=logical, request=request)
        label = f"{stack['name']}-{logical}-{request_type}"
        if rtype.startswith("Custom::") or rtype == "AWS::CloudFormation::CustomResource":
            response, run = self._custom(request, label)
        elif rtype == FUNCTION_TYPE:
            response, run = self._function(stack, request), None
        elif rtype in ("Local::Hook", "Local::Service"):
            response, run = self._builtin(request, label)
        else:
            response, run = ({"Status": "FAILED", "Reason": f"unsupported resource type {rtype}",
                              "PhysicalResourceId": physical or ""}, None)
        if not response.get("PhysicalResourceId"):
            response["PhysicalResourceId"] = physical or ""
        extra = {}
        if run is not None:
            extra = {"strays": run["strays"], "swept_files": run["swept_files"], "seconds": run["seconds"]}
        self.emit("response", stack=stack["name"], logical=logical, request_type=request_type,
                  request_id=request["RequestId"], response=response, **extra)
        return response

    def _custom(self, request, label):
        token = request["ResourceProperties"].get("ServiceToken", "")
        function = self.functions.get(token) if isinstance(token, str) else None
        if function is None:
            return {"Status": "FAILED", "Reason": f"ServiceToken {token!r} is not a deployed function"}, None
        # the code the function has at the moment the request is sent
        run = self.sandbox.run([sys.executable, "-m", function["handler"]], json.dumps(request), label=label,
                               pythonpath=function["code"])
        if run["timed_out"]:
            return {"Status": "FAILED", "Reason": f"handler did not respond within {self.sandbox.timeout}s"}, run
        try:
            response = json.loads(run["stdout"])
            if not isinstance(response, dict) or response.get("Status") not in ("SUCCESS", "FAILED"):
                raise ValueError("Status must be SUCCESS or FAILED")
        except ValueError as exc:
            tail = run["stderr"].strip().splitlines()[-5:]
            return {"Status": "FAILED", "Reason": f"invalid handler response ({exc}); stderr: {' | '.join(tail)}"}, run
        if request["RequestType"] == "Delete" and response.get("PhysicalResourceId") not in (None, request.get("PhysicalResourceId")):
            return {"Status": "FAILED", "Reason": "Delete must not change the PhysicalResourceId"}, run
        if response["Status"] == "SUCCESS" and request["RequestType"] != "Delete":
            data = response.get("Data") or {}
            if not isinstance(data, dict):
                return {"Status": "FAILED", "Reason": "Data must be an object"}, run
        return response, run

    def _builtin(self, request, label):
        props = request["ResourceProperties"]
        physical = request.get("PhysicalResourceId") or f"{request['LogicalResourceId']}-{uuid.uuid4().hex[:12]}"
        if request["RequestType"] == "Delete":
            return {"Status": "SUCCESS", "PhysicalResourceId": physical}, None
        command = props.get("Command") if request["ResourceType"] == "Local::Hook" else props.get("HealthCheck")
        if not command:
            return {"Status": "SUCCESS", "PhysicalResourceId": physical, "Data": {}}, None
        env = {str(k): str(v) for k, v in (props.get("Environment") or {}).items()}
        run = self.sandbox.run(list(command), "", extra_env=env, want_response=False, label=label)
        if run["timed_out"] or run["returncode"] != 0:
            tail = " | ".join(run["stderr"].strip().splitlines()[-3:])
            why = "timed out" if run["timed_out"] else f"exit status {run['returncode']}"
            return {"Status": "FAILED", "PhysicalResourceId": physical, "Reason": f"{why}: {tail}"}, run
        return {"Status": "SUCCESS", "PhysicalResourceId": physical, "Data": {}}, run

    def _delete_with_retries(self, stack, record, logical, attempts=CLEANUP_DELETE_ATTEMPTS):
        for attempt in range(1, attempts + 1):
            response = self._invoke(stack, logical, record["type"], "Delete", record["props"],
                                    physical=record["physical_id"])
            if response["Status"] == "SUCCESS":
                return True
        self.emit("abandoned", stack=stack["name"], logical=logical, physical_id=record["physical_id"],
                  attempts=attempts)
        return False

    # ------------------------------------------------------------------ stack operations

    def deploy(self, name, template):
        """Create or update stack ``name`` to ``template``; returns the final stack status."""
        template = self.publish(template)
        stack = self.stacks.get(name)
        if stack is None:
            stack = {
                "name": name,
                "id": f"arn:aws:cloudformation:{self.region}:{ACCOUNT_ID}:stack/{name}/{uuid.uuid4()}",
                "status": None, "status_reason": "", "template": None, "resources": {},
            }
            self.stacks[name] = stack
            return self._create(stack, template)
        if stack["status"] not in DEPLOYABLE:
            self.emit("refused", stack=name, status=stack["status"],
                      reason=f"stack {name} is in {stack['status']} and cannot be updated")
            return stack["status"]
        return self._update(stack, template)

    def _create(self, stack, template):
        self._status(stack, "CREATE_IN_PROGRESS")
        created, failure = [], None
        self.inflight = {"stack": stack["name"], "applied": {}}
        for logical, spec in template["Resources"].items():
            try:
                props, refs = self.resolve(spec.get("Properties", {}), stack["name"])
            except ResolveError as exc:
                failure = (logical, str(exc))
                break
            response = self._invoke(stack, logical, spec["Type"], "Create", props)
            record = self._record(spec, props, refs, response)
            created.append((logical, record))
            if response["Status"] != "SUCCESS":
                failure = (logical, response.get("Reason", ""))
                break
            self.inflight["applied"][logical] = record
        if failure is None:
            stack["resources"] = dict(created)
            stack["template"] = template
            self.inflight = None
            self._status(stack, "CREATE_COMPLETE")
            return stack["status"]
        self.inflight = None
        self._status(stack, "CREATE_FAILED", f"{failure[0]}: {failure[1]}")
        self._status(stack, "ROLLBACK_IN_PROGRESS")
        ok = True
        for logical, record in reversed(created):
            ok = self._delete_with_retries(stack, record, logical) and ok
        stack["resources"] = {}
        self._status(stack, "ROLLBACK_COMPLETE" if ok else "ROLLBACK_FAILED")
        return stack["status"]

    @staticmethod
    def _record(spec, props, refs, response):
        return {
            "type": spec["Type"],
            "template_props": copy.deepcopy(spec.get("Properties", {})),
            "props": props,
            "refs": refs,
            "physical_id": response.get("PhysicalResourceId", ""),
            "data": response.get("Data") or {},
        }

    def _update(self, stack, template):
        committed = stack["resources"]
        steps = []          # applied operations, in order: dicts with action/logical/old/new
        working = {}        # the resource set the stack will hold if the update commits
        failure = None
        changed = any(
            logical not in committed
            or committed[logical]["type"] != spec["Type"]
            or committed[logical]["template_props"] != spec.get("Properties", {})
            for logical, spec in template["Resources"].items()
        ) or any(logical not in template["Resources"] for logical in committed)
        if not changed:
            self.emit("no_changes", stack=stack["name"])
            return stack["status"]
        self._status(stack, "UPDATE_IN_PROGRESS")
        self.inflight = {"stack": stack["name"], "applied": {}}
        for logical, spec in template["Resources"].items():
            old = committed.get(logical)
            if old is not None and old["type"] == spec["Type"] and old["template_props"] == spec.get("Properties", {}):
                working[logical] = old
                continue
            try:
                props, refs = self.resolve(spec.get("Properties", {}), stack["name"])
            except ResolveError as exc:
                failure = (logical, str(exc))
                break
            if old is None or old["type"] != spec["Type"]:
                response = self._invoke(stack, logical, spec["Type"], "Create", props)
                step = {"action": "create", "logical": logical, "old": old,
                        "new": self._record(spec, props, refs, response)}
            else:
                response = self._invoke(stack, logical, spec["Type"], "Update", props,
                                        physical=old["physical_id"], old_props=old["props"])
                new = self._record(spec, props, refs, response)
                replaced = response["Status"] == "SUCCESS" and new["physical_id"] != old["physical_id"]
                if not replaced:
                    new["physical_id"] = old["physical_id"]
                step = {"action": "replace" if replaced else "update", "logical": logical, "old": old, "new": new}
            steps.append(step)
            if response["Status"] != "SUCCESS":
                failure = (logical, response.get("Reason", ""))
                break
            working[logical] = step["new"]
            self.inflight["applied"][logical] = step["new"]

        if failure is None:
            removed = [(logical, rec) for logical, rec in committed.items() if logical not in template["Resources"]]
            stack["resources"] = working
            stack["template"] = template
            self.inflight = None
            self._status(stack, "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS")
            for step in steps:
                if step["action"] == "replace" or (step["action"] == "create" and step["old"] is not None):
                    self._delete_with_retries(stack, step["old"], step["logical"])
            for logical, record in reversed(removed):
                self._delete_with_retries(stack, record, logical)
            self._status(stack, "UPDATE_COMPLETE")
            return stack["status"]

        self.inflight = None
        self.emit("update_failed", stack=stack["name"], logical=failure[0], reason=failure[1])
        self._status(stack, "UPDATE_ROLLBACK_IN_PROGRESS", f"{failure[0]}: {failure[1]}")
        cleanup = []
        for step in reversed(steps):
            if step["action"] in ("create", "replace"):
                cleanup.append((step["logical"], step["new"]))
                continue
            old, attempted = step["old"], step["new"]
            try:
                props, _ = self.resolve(old["template_props"], stack["name"])
            except ResolveError as exc:
                self._status(stack, "UPDATE_ROLLBACK_FAILED", f"{step['logical']}: {exc}")
                return stack["status"]
            response = self._invoke(stack, step["logical"], old["type"], "Update", props,
                                    physical=old["physical_id"], old_props=attempted["props"])
            if response["Status"] != "SUCCESS":
                self._status(stack, "UPDATE_ROLLBACK_FAILED", f"{step['logical']}: {response.get('Reason', '')}")
                return stack["status"]
        self._status(stack, "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS")
        for logical, record in cleanup:
            if record["physical_id"]:
                self._delete_with_retries(stack, record, logical)
        self._status(stack, "UPDATE_ROLLBACK_COMPLETE")
        return stack["status"]

    def delete_stack(self, name):
        stack = self.stacks.get(name)
        if stack is None:
            return "DELETE_COMPLETE"
        self._status(stack, "DELETE_IN_PROGRESS")
        ok = True
        for logical, record in reversed(list(stack["resources"].items())):
            if self._delete_with_retries(stack, record, logical):
                del stack["resources"][logical]
            else:
                ok = False
        if not ok:
            self._status(stack, "DELETE_FAILED")
            return stack["status"]
        self._status(stack, "DELETE_COMPLETE")
        del self.stacks[name]
        return "DELETE_COMPLETE"
