#!/usr/bin/env python3
"""reconciler -- drive one Route 53 hosted zone towards a desired endpoint list.

    reconciler once --endpoint-url URL --zone-id ID --owner-id ID \
                    --desired PATH --max-changes-per-call N

One invocation is one reconcile cycle: list the zone, decide what has to change,
submit it, print one JSON line {"applied": [...], "failed": [...]} on stdout.
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

Route 53 applies one ChangeResourceRecordSets request as a unit: either every
change in the batch lands or none of them does. A record and the claim that
makes it ours therefore have to travel in the same request, and whatever else
shares that request goes down with it when one of its changes is refused -- so
the pair, not the record, is the unit both of submission and of retry.

The report is accounted over the desired list, not over the change list: a
name that is not ours to touch is still an endpoint that is not in shape, and
it is reported for as long as that is true, exactly like one whose change was
refused.
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


def owner_of(zone, name):
    """The owner named by the claim on `name`, or None when there is no claim."""
    claim = zone.get(claim_name_for(name), {}).get("TXT")
    if claim is None:
        return None
    labels = parse_claim(values_of(claim)[0])
    if labels is None:
        return None
    return labels.get(OWNER_LABEL)


# -------------------------------------------------------------------- plan


def plan_changes(desired, zone, owner_id):
    """Return the list of changes that move the zone towards `desired`.

    A name is ours only while its claim names this owner. A name that carries
    somebody else's claim, or an unclaimed name somebody else is serving, is
    never written -- not even with a create, because a claim sitting on the
    name without its record means the other owner intends to keep it. A name of
    ours whose record went missing is re-created under the claim that is still
    there, and that claim is left exactly as it stands.
    """
    changes = []

    for endpoint in desired:
        name = endpoint["name"].lower().rstrip(".")
        rtype = endpoint.get("type", "A")
        ttl = int(endpoint.get("ttl", RECORD_TTL_DEFAULT))
        target = endpoint["targets"][0]
        current = zone.get(name, {}).get(rtype)
        claim_owner = owner_of(zone, name)
        claim = zone.get(claim_name_for(name), {}).get("TXT")

        if current is None:
            if claim is not None and claim_owner != owner_id:
                # claimed by another controller: the name is not free
                continue
            changes.append({"action": "CREATE", "name": name, "type": rtype,
                            "ttl": ttl, "values": [target]})
            if claim is None:
                # only a record whose claim we are establishing with it
                changes.append({"action": "CREATE", "name": claim_name_for(name),
                                "type": "TXT", "ttl": ttl,
                                "values": [claim_value({OWNER_LABEL: owner_id})]})
            continue

        if claim_owner != owner_id:
            # held by somebody else, or not claimed at all
            continue

        if values_of(current) != [target] or current.get("TTL") != ttl:
            changes.append({"action": "UPSERT", "name": name, "type": rtype,
                            "ttl": ttl, "values": [target]})

    held = set(endpoint["name"].lower().rstrip(".") for endpoint in desired)
    for name in sorted(zone):
        rs = zone[name].get("A")
        if rs is None or name in held:
            continue
        claim = zone.get(claim_name_for(name), {}).get("TXT")
        if claim is None:
            continue
        if owner_of(zone, name) != owner_id:
            continue
        changes.append({"action": "DELETE", "name": name, "type": "A",
                        "ttl": rs.get("TTL", RECORD_TTL_DEFAULT),
                        "values": values_of(rs)})
        # a delete has to name the value that is actually stored
        changes.append({"action": "DELETE", "name": claim_name_for(name),
                        "type": "TXT", "ttl": claim.get("TTL", RECORD_TTL_DEFAULT),
                        "values": [values_of(claim)[0]]})

    changes.sort(key=lambda c: (c["name"], c["type"]))
    return changes


# ----------------------------------------------------------------- submit


def group_key(change):
    """The name whose record and claim have to commit together."""
    return data_name_of(change["name"])


def group_changes(changes):
    groups = {}
    for change in changes:
        groups.setdefault(group_key(change), []).append(change)
    return groups


def record_names_in(changes):
    names = []
    for change in changes:
        target = group_key(change)
        if target not in names:
            names.append(target)
    return names


def render(change_list):
    return [
        {
            "Action": change["action"],
            "ResourceRecordSet": {
                "Name": fqdn(change["name"]),
                "Type": change["type"],
                "TTL": change["ttl"],
                "ResourceRecords": [{"Value": v} for v in change["values"]],
            },
        }
        for change in change_list
    ]


def send(client, zone_id, change_list):
    try:
        client.change_resource_record_sets(
            HostedZoneId=zone_id, ChangeBatch={"Changes": render(change_list)}
        )
        return None
    except Exception as exc:
        return str(exc)


def build_calls(client, zone_id, changes, max_changes):
    """Pack whole groups into calls, and when a call is refused, retry its groups.

    Every change belonging to one name is kept in one request, because Route 53
    commits a request as a whole and a claim that lands without its record is a
    claim on a name we do not own. Requests are filled up to the change limit
    with whole groups. A refused request is broken into its groups and each is
    sent on its own, so one name that can never commit costs exactly its own
    group and nothing else.
    """
    if not changes:
        return [], []

    groups = group_changes(changes)
    applied, failed = [], []

    ordered = []
    for key in sorted(groups):
        group = groups[key]
        if len(group) > max_changes:
            print("group %s needs %d changes, over the %d per call; skipping"
                  % (key, len(group), max_changes), file=sys.stderr)
            failed.extend(record_names_in(group))
            continue
        if ordered and len(ordered[-1]) + len(group) > max_changes:
            ordered.append([])
        if not ordered:
            ordered.append([])
        ordered[-1].extend(group)

    for call in ordered:
        if not call:
            continue
        error = send(client, zone_id, call)
        if error is None:
            applied.extend(record_names_in(call))
            continue

        print("change batch rejected: %s" % error, file=sys.stderr)
        members = group_changes(call)
        if len(members) < 2:
            failed.extend(record_names_in(call))
            continue

        for key in sorted(members):
            group = members[key]
            error = send(client, zone_id, group)
            if error is None:
                applied.extend(record_names_in(group))
            else:
                print("change for %s rejected, leaving it for a later cycle: %s"
                      % (key, error), file=sys.stderr)
                failed.extend(record_names_in(group))

    return applied, failed


# ----------------------------------------------------------------- account


def in_shape(endpoint, zone, owner_id):
    """True when the name serves what the endpoint list asks of it, under us."""
    name = endpoint["name"].lower().rstrip(".")
    rtype = endpoint.get("type", "A")
    ttl = int(endpoint.get("ttl", RECORD_TTL_DEFAULT))
    current = zone.get(name, {}).get(rtype)
    if current is None or values_of(current) != [endpoint["targets"][0]]:
        return False
    if current.get("TTL") != ttl:
        return False
    return owner_of(zone, name) == owner_id


def account(desired, zone, owner_id, applied):
    """Account for every endpoint on the desired list.

    A name whose changes committed this cycle is in shape. Every other name is
    judged on the listing: one that is not ours to touch is reported for as
    long as it fails to serve what is asked of it, and so is one whose change
    was refused -- the report owes the operator both, every cycle.
    """
    done, outstanding = [], []
    for endpoint in desired:
        name = endpoint["name"].lower().rstrip(".")
        if name in applied or in_shape(endpoint, zone, owner_id):
            done.append(name)
        else:
            outstanding.append(name)
    return done, outstanding


# ------------------------------------------------------------------- cycle


def run_cycle(args):
    desired = json.load(open(args.desired))
    client = make_client(args.endpoint_url)
    zone = list_zone(client, zone_id=args.zone_id)
    changes = plan_changes(desired, zone, args.owner_id)
    applied, _ = build_calls(
        client, args.zone_id, changes, args.max_changes_per_call
    )
    done, outstanding = account(desired, zone, args.owner_id, set(applied))
    report = {"applied": sorted(done), "failed": sorted(outstanding)}
    print(json.dumps(report))
    return 0 if not outstanding else 2


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
