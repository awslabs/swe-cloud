"""Body callback server for manual runs: acknowledges every start at once.

Usage: python3 tools/callback.py /tmp/body.sock
"""

import json
import os
import socket
import sys
import threading


def handle(conn):
    with conn:
        buf = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "START":
                    conn.sendall(b'{"granted": true}\n')


def main(path):
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(32)
    sys.stderr.write("callback server on %s\n" % path)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main(sys.argv[1])
