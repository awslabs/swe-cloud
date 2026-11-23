"""Verifier-side setup and driver for the reconciler run.

Creates the zone, seeds it, arms the run, drives the cycles and reads the zone
back. Everything the assertions need is collected into a `run` dictionary; no
expectation is expressed here.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request

import boto3

from cdk_seed.cfn_deploy import deploy

RELAY_PORT = 4567
DIRECT_URL = "http://127.0.0.1:4566"
RELAY_URL = "http://127.0.0.1:%d" % RELAY_PORT
RUN_DIR = "/run/r53relay"

ZONE_NAME = "example.test."
OWNER_ID = "cluster-a"
FOREIGN_OWNER_ID = "cluster-b"
N_DESIRED = 16
CYCLES = 3
CONVERGE_BY_CYCLE = 2
MAX_CHANGES_PER_CALL = 8
MAX_CALLS_PER_CYCLE = 40
CYCLE_TIMEOUT_S = 60
RECORD_TTL = 300

GONE_NAME = "gone.example.test."
THEIRS_NAME = "theirs.example.test."
TAKEN_VALUE = "192.0.2.10"
RACE_VALUE = "192.0.2.30"
FLAP_VALUE = "192.0.2.40"
STALE_SEED_VALUE = "10.0.9.7"

RECONCILER = "/app/reconciler.py"

_results = []


def note(group, name, ok, detail=""):
    _results.append({"group": group, "name": name, "ok": bool(ok), "detail": str(detail)[:800]})
    return bool(ok)


def results():
    return list(_results)


# ------------------------------------------------------------- determinism


def shuffle_seed(items, seed):
    items = list(items)
    state = (seed * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
    for i in range(len(items) - 1, 0, -1):
        state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        j = (state >> 33) % (i + 1)
        items[i], items[j] = items[j], items[i]
    return items


def roles(seed):
    names = shuffle_seed(["svc-%02d.example.test." % i for i in range(N_DESIRED)], seed)
    return {
        "restore": names[0],
        "claimed": names[1],
        "taken": names[2],
        "race": names[3],
        "flap": names[4],
        "stale": names[5],
        "plain": names[6:],
    }


def desired_endpoints(seed):
    endpoints = [
        {"name": "svc-%02d.example.test." % i, "type": "A", "targets": ["10.0.1.%d" % i], "ttl": RECORD_TTL}
        for i in range(N_DESIRED)
    ]
    return shuffle_seed(endpoints, seed ^ 0x5DEECE66D)


# ------------------------------------------------------------------- relay


def relay_alive():
    try:
        with urllib.request.urlopen("%s/_health" % RELAY_URL, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def restart_relay():
    subprocess.run(["pkill", "-f", "relay.py"], check=False)
    for _ in range(60):
        if not _port_busy(RELAY_PORT):
            break
        time.sleep(0.2)
    os.makedirs(RUN_DIR, exist_ok=True)
    with open(os.path.join(RUN_DIR, "calls.jsonl"), "w"):
        pass
    if os.path.exists(os.path.join(RUN_DIR, "policy.json")):
        os.unlink(os.path.join(RUN_DIR, "policy.json"))
    subprocess.Popen(
        ["python3", "/tests/relay.py"],
        stdout=open("/tmp/relay-verifier.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(120):
        if relay_alive():
            return True
        time.sleep(0.5)
    return False


def _port_busy(port):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def arm(seed):
    with open(os.path.join(RUN_DIR, "policy.json"), "w") as handle:
        json.dump({"limit": MAX_CHANGES_PER_CALL, "seed": seed, "armed": True}, handle)


def set_cycle(number):
    with open(os.path.join(RUN_DIR, "cycle"), "w") as handle:
        handle.write(str(number))


def relay_log():
    path = os.path.join(RUN_DIR, "calls.jsonl")
    calls = []
    if not os.path.exists(path):
        return calls
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                calls.append(json.loads(line))
            except ValueError:
                continue
    return calls


# ------------------------------------------------------------------- setup


def direct_client():
    return boto3.client(
        "route53",
        endpoint_url=DIRECT_URL,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def claim_value(owner, extra=None):
    parts = ["heritage=external-dns", "external-dns/owner=%s" % owner]
    for key in sorted(extra or {}):
        parts.append("%s=%s" % (key, extra[key]))
    return '"' + ",".join(parts) + '"'


def claim_name(name):
    head, _, rest = name.partition(".")
    return "a-%s.%s" % (head, rest)


def put(client, zone_id, action, name, rtype, ttl, values):
    client.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Changes": [
                {
                    "Action": action,
                    "ResourceRecordSet": {
                        "Name": name,
                        "Type": rtype,
                        "TTL": ttl,
                        "ResourceRecords": [{"Value": v} for v in values],
                    },
                }
            ]
        },
    )


def create_zone():
    client = direct_client()
    reference = "verifier-%d" % int(time.time() * 1000)
    cfn = boto3.client('cloudformation', endpoint_url=client.meta.endpoint_url,
                       region_name=client.meta.region_name)
    outputs = deploy(cfn, 'seed-' + reference,
                     Path(__file__).parent / 'cdk_seed/seed.template.json', ZoneName=ZONE_NAME)
    return outputs['ZoneId'].split('/')[-1]


def seed_zone(zone_id, roles_map):
    client = direct_client()
    seeds = {}

    seeds["restore_claim"] = claim_value(OWNER_ID, {"external-dns/resource": "seed/restore"})
    put(client, zone_id, "CREATE", claim_name(roles_map["restore"]), "TXT", RECORD_TTL, [seeds["restore_claim"]])

    seeds["claimed_claim"] = claim_value(FOREIGN_OWNER_ID, {"external-dns/resource": "seed/claimed"})
    put(client, zone_id, "CREATE", claim_name(roles_map["claimed"]), "TXT", RECORD_TTL, [seeds["claimed_claim"]])

    seeds["stale_claim"] = claim_value(OWNER_ID)
    put(client, zone_id, "CREATE", roles_map["stale"], "A", RECORD_TTL, [STALE_SEED_VALUE])
    put(client, zone_id, "CREATE", claim_name(roles_map["stale"]), "TXT", RECORD_TTL, [seeds["stale_claim"]])

    put(client, zone_id, "CREATE", roles_map["taken"], "A", RECORD_TTL, [TAKEN_VALUE])

    seeds["gone_claim"] = claim_value(OWNER_ID, {"external-dns/resource": "seed/gone"})
    put(client, zone_id, "CREATE", GONE_NAME, "A", RECORD_TTL, ["10.0.9.1"])
    put(client, zone_id, "CREATE", claim_name(GONE_NAME), "TXT", RECORD_TTL, [seeds["gone_claim"]])

    seeds["theirs_claim"] = claim_value(FOREIGN_OWNER_ID, {"external-dns/resource": "seed/theirs"})
    put(client, zone_id, "CREATE", THEIRS_NAME, "A", RECORD_TTL, ["192.0.2.20"])
    put(client, zone_id, "CREATE", claim_name(THEIRS_NAME), "TXT", RECORD_TTL, [seeds["theirs_claim"]])
    return seeds


def read_zone(zone_id):
    client = direct_client()
    zone = {}
    paginator = client.get_paginator("list_resource_record_sets")
    for page in paginator.paginate(HostedZoneId=zone_id):
        for rs in page.get("ResourceRecordSets", []):
            key = (rs["Name"].lower().rstrip("."), rs["Type"])
            zone[key] = rs
    return zone


def values_of(rs):
    return [r["Value"] for r in (rs or {}).get("ResourceRecords", [])]


# ------------------------------------------------------------------ driver


def run_cycles(zone_id, desired_path):
    outcomes = []
    for cycle in range(1, CYCLES + 1):
        set_cycle(cycle)
        workdir = tempfile.mkdtemp(prefix="cycle%d-" % cycle)
        command = [
            "python3", RECONCILER, "once",
            "--endpoint-url", RELAY_URL,
            "--zone-id", zone_id,
            "--owner-id", OWNER_ID,
            "--desired", desired_path,
            "--max-changes-per-call", str(MAX_CHANGES_PER_CALL),
        ]
        try:
            proc = subprocess.run(
                command,
                cwd=workdir,
                env=dict(os.environ, PYTHONPATH="/app"),
                capture_output=True,
                text=True,
                timeout=CYCLE_TIMEOUT_S,
            )
            outcomes.append(
                {
                    "cycle": cycle,
                    "rc": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "timed_out": False,
                    "command": command,
                }
            )
        except subprocess.TimeoutExpired as exc:
            outcomes.append(
                {
                    "cycle": cycle,
                    "rc": None,
                    "stdout": (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                    "stderr": "timed out after %ds" % CYCLE_TIMEOUT_S,
                    "timed_out": True,
                    "command": command,
                }
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return outcomes


def report_of(outcome):
    text = (outcome.get("stdout") or "").strip()
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        parsed = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None

    def norm(items):
        out = []
        for item in items if isinstance(items, list) else []:
            name = str(item).lower().rstrip(".")
            if name not in out:
                out.append(name)
        return out

    return {"applied": norm(parsed.get("applied")), "failed": norm(parsed.get("failed"))}
