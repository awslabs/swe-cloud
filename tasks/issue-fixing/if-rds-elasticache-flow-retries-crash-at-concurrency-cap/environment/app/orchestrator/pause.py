"""Named pause points for the fault-injection rig.

When SEAM_SOCKET is set, `pause_point` announces itself (name, pid and the facts passed in) on that
Unix socket and blocks until the rig answers. Unset, it returns immediately. Keep every call where
it is in the flow and keep the facts it reports.
"""

from __future__ import annotations

import json
import os
import socket


def pause_point(name: str, **facts: object) -> None:
    path = os.environ.get("SEAM_SOCKET")
    if not path:
        return
    message = {"seam": name, "pid": os.getpid(), **facts}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)
            client.sendall((json.dumps(message, default=str) + "\n").encode())
            client.recv(64)
    except OSError:
        return
