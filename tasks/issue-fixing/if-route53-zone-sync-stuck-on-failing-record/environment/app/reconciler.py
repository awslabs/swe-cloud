#!/usr/bin/env python3
"""reconciler -- drive one Route 53 hosted zone towards a desired endpoint list.

    reconciler once --endpoint-url URL --zone-id ID --owner-id ID \
                    --desired PATH --max-changes-per-call N

One invocation is one reconcile cycle:

  1. list every record set in the zone;
  2. compare it with the desired list and decide what has to change;
  3. submit the changes to Route 53;
  4. print one JSON line {"applied": [...], "failed": [...]} on stdout.

Exit status is 0 when everything planned was committed, 2 when some names could
not be committed this cycle, and 1 for anything the operator has to look at.

Ownership
---------
Every data record this tool manages is paired with a claim record named after
it: A svc-07.example.test. is claimed by TXT a-svc-07.example.test. The claim
value is a quoted, comma-separated label list whose first label is
heritage=external-dns and whose external-dns/owner label names the controller
that owns the name:

    "heritage=external-dns,external-dns/owner=cluster-a"

Several controllers share one zone and tell each other apart by that owner
label, so the zone is the only thing this tool may trust. It carries no state
between invocations: each cycle re-reads the zone and re-decides.
"""

import argparse
import json
import sys

import boto3
from botocore.config import Config

HERITAGE = "heritage=external-dns"
OWNER_LABEL = "external-dns/owner"
CLAIM_PREFIX = "a-"
RECORD_TTL_DEFAULT = 300


# ---------------------------------------------------------------- zone reads


def make_client(endpoint_url):
    return boto3.client(
        "route53",
        endpoint_url=endpoint_url,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"max_attempts": 1, "mode": "standard"}),
    )


def list_zone(client, zone_id):
    """Return {name: {type: resource_record_set}} with names normalised."""
    zone = {}
    paginator = client.get_paginator("list_resource_record_sets")
    for page in paginator.paginate(HostedZoneId=zone_id):
        for rs in page.get("ResourceRecordSets", []):
            name = rs["Name"].lower().rstrip(".")
            zone.setdefault(name, {})[rs["Type"]] = rs
    return zone


def values_of(rs):
    return [r["Value"] for r in rs.get("ResourceRecords", [])]


def fqdn(name):
    return name if name.endswith(".") else name + "."


def claim_name_for(name):
    labels = name.split(".")
    if len(labels) < 2:
        return CLAIM_PREFIX + name
    return CLAIM_PREFIX + labels[0] + "." + ".".join(labels[1:])


def data_name_of(name):
    if name.startswith(CLAIM_PREFIX):
        rest = name[len(CLAIM_PREFIX) :]
        parts = rest.split(".", 1)
        if len(parts) == 2:
            return parts[0] + "." + parts[1]
    return name


def parse_claim(raw):
    """Parse a claim value into labels, or None if it is not one of ours."""
    if raw is None:
        return None
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    labels = {}
    for part in text.split(","):
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        labels[key.strip()] = value.strip()
    if labels.get("heritage") != "external-dns":
        return None
    return labels


def claim_value(labels):
    parts = ["heritage=external-dns"]
    for key in sorted(k for k in labels if k != "heritage"):
        parts.append("%s=%s" % (key, labels[key]))
    return '"' + ",".join(parts) + '"'


# -------------------------------------------------------------------- plan


def plan_changes(desired, zone, owner_id):
    """Return the list of changes that move the zone towards `desired`.

    A name this owner already holds may be updated or removed; a name that is
    free gets created; a name somebody else is holding is left alone.
    """
    changes = []

    for endpoint in desired:
        name = endpoint["name"].lower().rstrip(".")
        rtype = endpoint.get("type", "A")
        ttl = int(endpoint.get("ttl", RECORD_TTL_DEFAULT))
        target = endpoint["targets"][0]
        current = zone.get(name, {}).get(rtype)
        claim = zone.get(claim_name_for(name), {}).get("TXT")
        claim_labels = parse_claim(values_of(claim)[0]) if claim else None

        if current is None:
            # nobody is serving this name yet
            changes.append({"action": "CREATE", "name": name, "type": rtype,
                            "ttl": ttl, "values": [target]})
            changes.append({"action": "CREATE", "name": claim_name_for(name),
                            "type": "TXT", "ttl": ttl,
                            "values": [claim_value({OWNER_LABEL: owner_id})]})
            continue

        if claim_labels is None or claim_labels.get(OWNER_LABEL) != owner_id:
            # held by somebody else, or not claimed at all
            continue

        if values_of(current) != [target] or current.get("TTL") != ttl:
            changes.append({"action": "UPSERT", "name": name, "type": rtype,
                            "ttl": ttl, "values": [target]})

    held = set(endpoint["name"].lower().rstrip(".") for endpoint in desired)
    for name, by_type in sorted(zone.items()):
        rs = by_type.get("A")
        if rs is None:
            continue
        claim = zone.get(claim_name_for(name), {}).get("TXT")
        claim_labels = parse_claim(values_of(claim)[0]) if claim else None
        if name in held:
            continue
        if claim_labels is None or claim_labels.get(OWNER_LABEL) != owner_id:
            continue
        changes.append({"action": "DELETE", "name": name, "type": "A",
                        "ttl": rs.get("TTL", RECORD_TTL_DEFAULT),
                        "values": values_of(rs)})
        changes.append({"action": "DELETE", "name": claim_name_for(name),
                        "type": "TXT", "ttl": rs.get("TTL", RECORD_TTL_DEFAULT),
                        "values": [values_of(claim)[0]]})

    changes.sort(key=lambda c: (c["name"], c["type"]))
    return changes


# ----------------------------------------------------------------- submit


def record_names_in(changes):
    names = []
    for change in changes:
        target = data_name_of(change["name"]) if change["type"] == "TXT" else change["name"]
        if target not in names:
            names.append(target)
    return names


def submit_changes(client, zone_id, changes, max_changes):
    """Send the change list to Route 53 and report what committed."""
    if not changes:
        return [], []

    batch = [
        {
            "Action": change["action"],
            "ResourceRecordSet": {
                "Name": fqdn(change["name"]),
                "Type": change["type"],
                "TTL": change["ttl"],
                "ResourceRecords": [{"Value": v} for v in change["values"]],
            },
        }
        for change in changes
    ]

    try:
        client.change_resource_record_sets(
            HostedZoneId=zone_id, ChangeBatch={"Changes": batch}
        )
    except Exception as exc:
        print("change batch rejected: %s" % exc, file=sys.stderr)
        return [], record_names_in(changes)

    return record_names_in(changes), []


# ------------------------------------------------------------------- cycle


def run_cycle(args):
    desired = json.load(open(args.desired))
    client = make_client(args.endpoint_url)
    zone = list_zone(client, zone_id=args.zone_id)
    changes = plan_changes(desired, zone, args.owner_id)
    applied, failed = submit_changes(
        client, args.zone_id, changes, args.max_changes_per_call
    )
    report = {"applied": sorted(applied), "failed": sorted(failed)}
    print(json.dumps(report))
    return 0 if not failed else 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="reconciler")
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("once", help="run a single reconcile cycle")
    once.add_argument("--endpoint-url", required=True)
    once.add_argument("--zone-id", required=True)
    once.add_argument("--owner-id", required=True)
    once.add_argument("--desired", required=True)
    once.add_argument("--max-changes-per-call", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        return run_cycle(args)
    except Exception as exc:
        print("reconcile cycle failed: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
