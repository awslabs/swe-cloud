"""Custom::CrossRegionExportReader handler (reference).

Every Create/Update returns a fresh physical id that holds all of the state's imports, so an
Update is a replacement: the previous physical id is released by the cleanup Delete after the
consumer update commits, and the new one by the rollback-cleanup Delete if it rolls back.  The
release reclaims parameters the producer no longer exports.  A reader deployed by the previous
handlers kept the Create RequestId as its id and held per stack; its Delete releases that hold.

The rollback-cleanup Delete of a new id is not always ours to answer: when the deploy that rolled
back also brought this code, the function has been rolled back to the previous handlers by then, and
their Delete releases nothing of ours.  So every id an Update mints is recorded with the id it was
minted from; the engine addressing that parent again (another Update) shows the minted id was never
adopted, and its holds are released there.  A Delete of the parent shows it was adopted.
"""
import sys

import boto3

from . import entrypoint, ledger


def handler(event):
    props = event["ResourceProperties"]["ReaderProps"]
    imports = props["imports"]
    ssm = boto3.client("ssm", region_name=props["region"])
    request_type = event["RequestType"]
    if request_type in ("Create", "Update"):
        holder = f"{props['prefix']}-{event['RequestId']}"
        parent = event.get("PhysicalResourceId")
        for name in imports:
            if ledger.current_value(ssm, name) is None:
                raise Exception(f"Error importing {name}: parameter does not exist")
        orphans = {}

        def mint(gens):
            orphans.clear()
            orphans.update({g: info for g, info in gens.items() if parent and info["parent"] == parent and g != holder})
            gens[holder] = {"parent": parent, "imports": sorted(imports)}

        ledger.generations(event, mint)
        for name in imports:
            ledger.modify(name, lambda record: record["holders"].append(holder))
        for orphan, info in orphans.items():
            release(ssm, orphan, info["imports"])
            ledger.generations(event, lambda gens: gens.pop(orphan, None))
        return {"PhysicalResourceId": holder, "Data": imports}
    if request_type == "Delete":
        physical = event["PhysicalResourceId"]
        holder = physical if physical.startswith(f"{props['prefix']}-") else ledger.legacy_holder(props["prefix"])
        known = {}

        def retire(gens):
            known.update(gens.pop(physical, None) or {})
            for info in gens.values():
                if info["parent"] == physical:
                    info["parent"] = None

        names = set(imports)
        ledger.generations(event, lambda gens: known.update(gens.get(physical) or {}))
        names.update(known.get("imports", []))
        release(ssm, holder, sorted(names))
        ledger.generations(event, retire)
    return None


def release(ssm, holder, names):
    names = [name for name in names if ledger.get(name) is not None]
    for name in names:
        ledger.modify(name, lambda record: record.update(holders=[h for h in record["holders"] if h != holder]))
    reclaimed = ledger.reclaim(ssm, names)
    print(f"released {holder}; reclaimed {reclaimed}", file=sys.stderr)


if __name__ == "__main__":
    entrypoint.run(handler)
