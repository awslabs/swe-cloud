"""Custom-resource entrypoint: one request on stdin, one response line on stdout.

Port of the CDK custom-resource-provider entrypoint.  A handler returns ``None`` or a dict that may
carry ``PhysicalResourceId`` and ``Data``; exceptions become a FAILED response whose Reason is the
error message.
"""
import json
import sys
import traceback


def render_response(event, result):
    result = result or {}
    physical = result.get("PhysicalResourceId") or event.get("PhysicalResourceId") or event["RequestId"]
    if event["RequestType"] == "Delete" and physical != event.get("PhysicalResourceId"):
        raise ValueError("DELETE: cannot change the physical resource ID from "
                         f"{event.get('PhysicalResourceId')} to {physical} during deletion")
    return {
        "Status": "SUCCESS",
        "Reason": "SUCCESS",
        "PhysicalResourceId": physical,
        "StackId": event.get("StackId"),
        "RequestId": event.get("RequestId"),
        "LogicalResourceId": event.get("LogicalResourceId"),
        "Data": result.get("Data") or {},
    }


def run(handler):
    event = json.loads(sys.stdin.read())
    try:
        response = render_response(event, handler(event))
    except Exception as exc:  # noqa: BLE001 - every failure must reach the engine as FAILED
        traceback.print_exc(file=sys.stderr)
        response = {
            "Status": "FAILED",
            "Reason": f"{type(exc).__name__}: {exc}",
            "PhysicalResourceId": event.get("PhysicalResourceId") or event["RequestId"],
            "StackId": event.get("StackId"),
            "RequestId": event.get("RequestId"),
            "LogicalResourceId": event.get("LogicalResourceId"),
            "Data": {},
        }
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()
