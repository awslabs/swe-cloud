"""Control pipe and body-callback client.

Every process is launched with a control file descriptor.  It writes one JSON
line there when it is ready and then keeps reading JSON lines from it for the
rest of its life:

  STOP                              shut down cleanly
  COMPLETE                          complete an execution through the normal
                                    {"run_id", "claim_token", "result"} path

Bodies are not executed in the dark: before doing any work the worker announces
the attempt over the callback socket as {"type": "START", ...} and waits for the
acknowledgement.  That pause is part of the contract - the launcher may hold an
acknowledgement for as long as it likes, so the worker must keep its claim
maintained while it waits, and a body must never be considered finished before
the callback returns.
"""

import json
import select
import socket
import time


class Control:
    """Line protocol over the inherited control descriptor.

    The launcher hands the process one end of a socket pair; JSON lines travel
    both ways on it.
    """

    def __init__(self, fd):
        self.sock = socket.socket(fileno=fd)
        self.sock.settimeout(None)
        self._buf = b""
        self.closed = False

    def send(self, **msg):
        self.sock.sendall((json.dumps(msg) + "\n").encode())

    def recv(self, timeout=None):
        if self.closed:
            time.sleep(timeout or 0.0)
            return None
        if timeout is not None:
            ready, _, _ = select.select([self.sock], [], [], timeout)
            if not ready:
                return None
        while b"\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                self.closed = True
                time.sleep(timeout or 0.0)
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line) if line.strip() else None


class Callback:
    """Client side of the body callback: announce the attempt, wait, work.

    The connection is held open for the whole body so that the holder sees
    both the start and the return of the work it permitted.
    """

    def __init__(self, path):
        self.path = path

    def run(self, job, slot, run_id, claim_token, pid, body):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(None)          # the holder decides when work starts
            sock.connect(self.path)
            payload = {
                "type": "START",
                "job": job,
                "slot": int(slot),
                "run_id": run_id,
                "claim_token": claim_token,
                "pid": pid,
            }
            sock.sendall((json.dumps(payload) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                raise RuntimeError("callback closed before acknowledging")
            reply = json.loads(buf.decode())
            if not reply.get("granted", True):
                return reply
            body()
            sock.sendall(b'{"type": "END"}\n')
            return reply
