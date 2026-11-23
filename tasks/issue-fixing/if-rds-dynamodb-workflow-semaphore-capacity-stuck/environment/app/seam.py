import json
import os
import socket


def pause(point, **facts):
    path = os.environ.get("SEAM_SOCKET")
    if not path:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(path)
        client.sendall((json.dumps({"seam": point, **facts}) + "\n").encode())
        client.recv(64)
