"""Local Lambda runtime adapter: one invocation per process.

    python3 -m control_plane.runtime control_plane.lambda_handler.scale_up_handler < event.json

Reads the invocation event (JSON) from stdin, calls the handler with a Lambda-style context and
writes the handler's return value as JSON to stdout. An exception escaping the handler is a
function error: the traceback goes to stderr and the process exits 1. The deadline comes from
LAMBDA_DEADLINE_MS (epoch milliseconds, set by whoever invokes the function) or, failing that, from
LAMBDA_TIMEOUT_S after start. The invoker kills the process at the deadline.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import uuid


class LambdaContext:
    def __init__(self, function_name: str, deadline_ms: int) -> None:
        self.function_name = function_name
        self.aws_request_id = str(uuid.uuid4())
        self.invoked_function_arn = f"arn:aws:lambda:us-east-1:000000000000:function:{function_name}"
        self._deadline_ms = deadline_ms

    def get_remaining_time_in_millis(self) -> int:
        return max(0, self._deadline_ms - int(time.time() * 1000))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: python3 -m control_plane.runtime <module>.<function> < event.json\n")
        return 2
    module_name, _, function_name = argv[1].rpartition(".")
    handler = getattr(importlib.import_module(module_name), function_name)
    deadline_ms = int(os.environ.get("LAMBDA_DEADLINE_MS") or 0) or int(
        (time.time() + float(os.environ.get("LAMBDA_TIMEOUT_S") or 8)) * 1000
    )
    event = json.loads(sys.stdin.read() or "{}")
    result = handler(event, LambdaContext(os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "ghr-scale-up"), deadline_ms))
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
