"""Black-box verifier for /app/orphan_clean.py.

Each case is a fresh table prefix in a fresh, unversioned bucket. The cleaner runs as its own
process, pointed at the gateway, while the scenario commits and expires through the gateway's
hooks. Graded: every delete when it was forwarded (never one a present snapshot references), the
end state of every present snapshot, the orphans, and the exit code within the deadline.
"""

import json
from pathlib import Path

import boto3
from cdk_seed.cfn_deploy import deploy
import os
import random
import subprocess
import sys
import tempfile
import time
import uuid

import pytest
from botocore.exceptions import ClientError

import lakeharness as h

APP_DIR = os.environ.get("VERIFIER_APP_DIR", "/app")  # overridable only for authoring dry runs
GATEWAY_PORT = 4570
DEADLINE_S = 60
CUTOFF_GUARD_S = 2.2

CASES = {
    "static-0": lambda: h.Static(),
    "static-1": lambda: h.Static(),
    "list-0": lambda: h.ListThenExpire(),
    "list-1": lambda: h.ListThenExpire(),
    "list-2": lambda: h.ListThenExpire(),
    "relist-0": lambda: h.ReadThenExpire(),
    "mlist-0": lambda: h.MergeThenExpire("list", 700),
    "mlist-1": lambda: h.MergeThenExpire("manifest", 1300),
    "mlist-2": lambda: h.MergeThenExpire("list", 1500),
    "lost-0": lambda: h.LostWhileCommitting("lists"),
    "lost-1": lambda: h.LostWhileCommitting("manifest"),
    "lost-2": lambda: h.LostWhileCommitting("base"),
    "missing-0": lambda: h.Missing("shared"),
    "missing-1": lambda: h.Missing("s1-base"),
    "missing-2": lambda: h.Missing("shared-last"),
    "churn-0": lambda: h.Churn(700),
    "churn-1": lambda: h.Churn(1300),
    "churn-2": lambda: h.Churn(1500),
    "empty-0": lambda: h.Empty(),
}


class Env:
    def __init__(self):
        self.s3 = h.direct_client()
        self.bucket = f"lake-verify-{uuid.uuid4().hex[:10]}"
        self.rng = random.Random()
        self.tables = {}
        self.hung = set()
        self.cutoff_ms = None
        self.gateway = None


@pytest.fixture(scope="session")
def env():
    e = Env()
    cfn = boto3.client("cloudformation", endpoint_url=e.s3.meta.endpoint_url,
                       region_name=e.s3.meta.region_name,
                       aws_access_key_id="test", aws_secret_access_key="test")
    deploy(cfn, "seed-" + e.bucket, Path(__file__).parent / "cdk_seed/seed.template.json",
           BucketName=e.bucket)
    order = list(CASES)
    e.rng.shuffle(order)  # no prefix order to lean on
    for case in order:
        scenario = CASES[case]()
        table = h.Table(e.s3, e.bucket, f"tables/t{uuid.uuid4().hex[:12]}/", random.Random(e.rng.random()))
        if not isinstance(scenario, h.Empty):
            table.build_history()
        table.add_orphans()
        scenario.setup(table)
        e.tables[case] = (table, scenario)
    time.sleep(CUTOFF_GUARD_S)
    e.cutoff_ms = int(time.time() * 1000)
    time.sleep(CUTOFF_GUARD_S)
    for port in range(GATEWAY_PORT, GATEWAY_PORT + 40):
        try:
            e.gateway = h.Gateway(e.bucket, port)
            break
        except OSError:
            continue  # something left listening there; the port is not part of the contract
    assert e.gateway is not None, "no free port for the gateway"
    e.gateway.start()
    yield e
    e.gateway.stop()


def run_cleaner(e, table):
    env = dict(os.environ)
    env.update(PYTHONPATH=APP_DIR, AWS_ENDPOINT_URL=e.gateway.url, AWS_DEFAULT_REGION="us-east-1",
               AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test")
    cmd = [sys.executable, os.path.join(APP_DIR, "orphan_clean.py"), "--endpoint-url", e.gateway.url, "--bucket", e.bucket,
           "--table-prefix", table.prefix, "--older-than-ms", str(e.cutoff_ms)]
    started = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=tempfile.mkdtemp(prefix="cleaner-"), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=DEADLINE_S)
        return proc.returncode, out, err, time.monotonic() - started
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return None, out, err, time.monotonic() - started


def exists(e, table, path):
    try:
        e.s3.head_object(Bucket=e.bucket, Key=table.prefix + path)
        return True
    except ClientError as err:
        if h.not_found(err):
            return False
        raise


def get_json(e, table, path):
    try:
        return json.loads(e.s3.get_object(Bucket=e.bucket, Key=table.prefix + path)["Body"].read())
    except ClientError as err:
        if h.not_found(err):
            return None
        raise


def end_state_problems(e, table):
    """Walk every snapshot file present in S3, and the model's references of each."""
    problems = []
    listed = e.s3.list_objects_v2(Bucket=e.bucket, Prefix=table.prefix + "snapshot/").get("Contents", [])
    ids = set()
    for item in listed:
        name = item["Key"][len(table.prefix + "snapshot/"):]
        if name.startswith("snapshot-") and name[len("snapshot-"):].isdigit():
            ids.add(int(name[len("snapshot-"):]))
    if ids != table.present:
        problems.append(f"snapshot files present {sorted(ids)}, expected {sorted(table.present)}")
    for sid in sorted(ids):
        snap = get_json(e, table, f"snapshot/snapshot-{sid}")
        if snap is None:
            continue
        for list_name in (snap["baseManifestList"], snap["deltaManifestList"]):
            lst = get_json(e, table, "manifest/" + list_name)
            if lst is None:
                if "manifest/" + list_name not in table.removed:
                    problems.append(f"snapshot {sid}: manifest list {list_name} is gone")
                continue
            for m in lst["manifests"]:
                man = get_json(e, table, "manifest/" + m)
                if man is None:
                    if "manifest/" + m not in table.removed:
                        problems.append(f"snapshot {sid}: manifest {m} is gone")
                    continue
                for f in man["files"]:
                    if not exists(e, table, f):
                        problems.append(f"snapshot {sid}: data file {f} is gone")
    for sid in sorted(table.present & ids):
        for path in sorted(table.refs(sid) - table.removed):
            if not exists(e, table, path):
                problems.append(f"snapshot {sid} references {path}, which is gone")
    return sorted(set(problems))


@pytest.mark.parametrize("case", list(CASES))
def test_orphan_clean(env, case):
    family = case.rsplit("-", 1)[0]
    if family in env.hung:
        pytest.fail(f"an earlier {family} run was still running after {DEADLINE_S}s; not repeated")
    table, scenario = env.tables[case]
    run = env.gateway.begin(table, scenario)
    code, out, err, elapsed = run_cleaner(env, table)
    run.wait_tails()
    env.gateway.end()

    problems = []
    if code is None:
        env.hung.add(family)
        problems.append(f"still running after {DEADLINE_S}s; killed")
    problems += [f"harness: {x}" for x in run.errors]
    problems += [f"unsafe delete: {v}" for v in run.violations]
    if table.snapshots and not run.read_snapshot_file:
        problems.append("no snapshot file was read through --endpoint-url")
    problems += end_state_problems(env, table)

    deleted = {k for k, _ in run.deletes}
    if scenario.expect == "clean":
        if code is not None and code != 0:
            problems.append(f"exit code {code}, expected 0 (table is readable)")
        for path in sorted(table.orphans):
            if exists(env, table, path):
                problems.append(f"orphan {path} was not removed")
            elif path not in deleted:
                problems.append(f"orphan {path} vanished without a DELETE through --endpoint-url")
    else:
        if code is not None and code != 3:
            problems.append(f"exit code {code}, expected 3 (a retained snapshot's files are missing)")
        data_or_manifest = sorted(k for k in deleted if k.startswith(("bucket-0/", "manifest/")))
        if data_or_manifest:
            problems.append(f"{len(data_or_manifest)} DELETEs under bucket-0/ or manifest/ "
                            f"although the used files cannot be determined, e.g. {data_or_manifest[:3]}")
        for path in sorted(table.orphans):
            if not exists(env, table, path):
                problems.append(f"orphan {path} removed although the run had to delete nothing")

    print(json.dumps({"case": case, "exit": code, "elapsed_s": round(elapsed, 2), "gets": run.gets,
                      "deletes": len(run.deletes), "refused_writes": len(run.refused_writes),
                      "commits": getattr(scenario, "commits", None), "problems": len(problems)}))
    if problems:
        unsafe = [p for p in problems if p.startswith("unsafe delete")]
        rest = [p for p in problems if not p.startswith("unsafe delete")]
        shown = unsafe[:8] + rest[:12]
        hidden = len(problems) - len(shown)
        detail = "\n".join(shown + ([f"... and {hidden} more"] if hidden else []))
        tail = "\n".join((out or "").strip().splitlines()[-3:] + (err or "").strip().splitlines()[-15:])
        pytest.fail(f"{case}: {len(problems)} problem(s)\n{detail}\n--- cleaner output (tail) ---\n{tail}")
