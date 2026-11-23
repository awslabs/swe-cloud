"""Pause points used by the deploy harness.

The service announces that it has reached a named point in its flow and
waits for the harness to let it continue. Unset SEAM_SOCKET (normal
operation outside the harness) makes this a no-op.
"""
import json
import os
import socket


def seam(name, **facts):
    path = os.environ.get("SEAM_SOCKET")
    if not path:
        return
    payload = json.dumps({"seam": name, **facts})
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(300.0)
        client.connect(path)
        client.sendall((payload + "\n").encode())
        # The harness reply; a killed placement never receives one.
        client.recv(64)
