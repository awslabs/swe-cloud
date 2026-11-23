"""Optional launcher instrumentation for process handoffs."""
import json
import os
import socket


def rendezvous(stage, **facts):
    address = os.getenv("FEED_CONTROL_SOCKET")
    if not address:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(address)
        connection.sendall((json.dumps({"stage": stage, **facts}) + "\n").encode())
        if not connection.recv(64):
            raise RuntimeError("launcher disconnected")
