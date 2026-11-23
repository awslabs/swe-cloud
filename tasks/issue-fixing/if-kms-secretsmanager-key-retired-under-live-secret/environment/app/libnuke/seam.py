"""Named pause points. With SEAM_SOCKET unset (normal operation) they do nothing.

When SEAM_SOCKET names a unix socket, the process announces the point and blocks until the
driver on the other end acknowledges. Any reimplementation of the run loop must keep
announcing the same points with the same facts.
"""
import json
import os
import socket


def seam(name, **facts):
    path = os.environ.get("SEAM_SOCKET")
    if not path:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(path)
        client.sendall((json.dumps({"seam": name, **facts}) + "\n").encode())
        client.recv(64)
