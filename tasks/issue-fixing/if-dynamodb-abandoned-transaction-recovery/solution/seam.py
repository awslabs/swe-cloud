"""Announce a named point in the flow and block until the driver releases.

With SEAM_SOCKET unset the program runs without pausing; the control
plane that drives the program sets SEAM_SOCKET to a unix socket path.
The announcement carries the facts the driver acts on, and the program
resumes only when the driver answers on the same connection.
"""

import decimal
import json
import os
import socket


def _plain(value):
    if isinstance(value, decimal.Decimal):
        return int(value)
    return str(value)


def seam(name, **facts):
    path = os.environ.get("SEAM_SOCKET")
    if not path:
        return
    payload = {"seam": name, "pid": os.getpid()}
    payload.update(facts)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(path)
        client.sendall((json.dumps(payload, default=_plain) + "\n").encode())
        client.recv(64)
