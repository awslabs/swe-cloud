"""Control channel: named pauses the driver can hold and release.

A worker announces that it has reached a point in its flow and then
blocks until the driver on the other end of the socket acknowledges.
With no control socket configured the calls return immediately, so
ordinary runs never pause.

The pause names and their fact fields are part of the deployed
contract.  A worker must keep announcing every pause listed in
README.md, with the same name and the same facts, at the same points
in its flow.
"""

import json
import socket


def announce(path: str, name: str, facts: dict) -> None:
    if not path:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(path)
        client.sendall(
            (json.dumps({"pause": name, **facts}) + "\n").encode("utf-8")
        )
        client.recv(64)  # the acknowledgement; a kill never sends one
