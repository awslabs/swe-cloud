"""Run one scheduler and one worker by hand, on a private table.

Happy path only: it creates a table, installs one job, starts the body
callback, one scheduler and one worker, and prints what they say until it is
interrupted.  Nothing here observes, judges or interrupts the processes.

  python3 tools/standalone.py --job demo --slots 4
  python3 tools/standalone.py --job demo --slots 4 --arm-after 8
"""

import argparse
import os
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/app")
import proto                                             # noqa: E402
from proto import State, now                            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def callback_server(path):
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(16)

    def serve():
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

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
                    if b'"START"' in line:
                        conn.sendall(b'{"granted": true}\n')

    threading.Thread(target=serve, daemon=True).start()


def spawn(prog, label, table, extra=()):
    read, write = socket.socketpair()
    log = open("/tmp/standalone_%s.log" % label, "ab")
    proc = subprocess.Popen(
        [sys.executable, "/app/%s.py" % prog, "--id", label,
         "--state-table", table, "--control-fd", str(write.fileno())] + list(extra),
        cwd="/tmp", stdout=log, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONPATH": "/app"}, pass_fds=(write.fileno(),))
    write.close()

    def reader():
        while True:
            line = read.makefile("r").readline()
            if not line:
                return
            sys.stdout.write("[%s ctrl] %s" % (label, line))

    threading.Thread(target=reader, daemon=True).start()
    return proc


def main():
    ap = argparse.ArgumentParser(description="one scheduler, one worker, one table")
    ap.add_argument("--job", default="demo")
    ap.add_argument("--slots", type=int, default=4)
    ap.add_argument("--period", type=float, default=proto.CONFIG["poll_s"] * 40)
    ap.add_argument("--arm-after", type=float, default=0.0,
                    help="seconds to wait before writing the anchor")
    ap.add_argument("--table", default="cadence_demo")
    args = ap.parse_args()

    st = State(args.table)
    st.create()
    st.put("META", "JOB#%s" % args.job, {
        "job": args.job, "period_s": args.period, "start_at": now() + 2,
        "concurrency": "forbid", "targets": ["w1"]})
    path = "/tmp/standalone_%d.sock" % os.getpid()
    callback_server(path)
    procs = [spawn("scheduler", "s1", args.table),
             spawn("worker", "w1", args.table, ("--callback-socket", path))]
    if args.arm_after > 0:
        time.sleep(args.arm_after)
    st.put("META", "ARM", {"anchor": now(), "slots": args.slots})
    sys.stdout.write("table=%s job=%s slots=%d period=%.1fs; logs in "
                     "/tmp/standalone_s1.log, kill with Ctrl-C\n"
                     % (args.table, args.job, args.slots, args.period))
    sys.stdout.flush()
    try:
        while True:
            for proc in procs:
                if proc.poll() is not None:
                    sys.stdout.write("%s exited\n" % proc.args)
                    return
            time.sleep(0.5)
    except KeyboardInterrupt:
        for proc in procs:
            proc.terminate()


if __name__ == "__main__":
    main()
