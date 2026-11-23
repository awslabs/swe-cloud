"""Custom::CrossRegionExportWriter handler (reference).

Publishes one SSM String parameter per export.  Decisions are taken against the parameter and the
ledger, never against the props diff alone, so the swapped Update CloudFormation sends on rollback
converges: re-adding an export that is still held, or "changing" a value back to what SSM already
holds, is a no-op.  An export that is dropped is marked not-exported and deleted as soon as no
reader holds it; otherwise the last holder's cleanup Delete reclaims it.
"""
import sys

import boto3

from . import entrypoint, ledger


def handler(event):
    props = event["ResourceProperties"]["WriterProps"]
    exports = props["exports"]
    ssm = boto3.client("ssm", region_name=props["region"])
    request_type = event["RequestType"]
    if request_type == "Create":
        publish(ssm, exports, {})
    elif request_type == "Update":
        old_exports = event["OldResourceProperties"]["WriterProps"]["exports"]
        publish(ssm, exports, old_exports)
        retire(ssm, [name for name in old_exports if name not in exports])
    elif request_type == "Delete":
        retire(ssm, list(exports))
    return None


def publish(ssm, exports, previous):
    targets = {name: value for name, value in exports.items() if previous.get(name) != value}
    current = {name: ledger.current_value(ssm, name) for name in targets}
    refused = []
    for name, value in targets.items():
        record = ledger.get(name)
        if record and record["holders"] and current[name] is not None and current[name] != value:
            refused.append(f"{name} is in use by {', '.join(record['holders'])} with value {current[name]!r}")
    if refused:
        raise Exception("Exports cannot be updated: \n" + "\n".join(refused))
    for name, value in targets.items():
        if current[name] != value:
            ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
        ledger.modify(name, lambda record: record.update(exported=True))
        print(f"exported {name}", file=sys.stderr)


def retire(ssm, names):
    for name in names:
        ledger.modify(name, lambda record: record.update(exported=False))
    reclaimed = ledger.reclaim(ssm, names)
    for name in names:
        print(f"{'deleted' if name in reclaimed else 'retired (still held)'} {name}", file=sys.stderr)


if __name__ == "__main__":
    entrypoint.run(handler)
