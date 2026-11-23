"""The worker process: takes admitted attempts and runs their bodies.

A worker only ever runs work the scheduler admitted and the job names it for.
While a body runs the worker keeps the claim maintained on a thread of its own,
so a body that is held for a long time does not starve it.  The control pipe is
read on that same thread: a completion that arrives from outside is applied by
the same path the worker's own bodies use.
"""

import argparse
import os
import sys
import threading
import time

import executions
from botocore.exceptions import ClientError
from control import Callback, Control
from proto import CONFIG, STATE_TABLE_DEFAULT, State, cond_failed, now


def log(fmt, *a):
    sys.stdout.write("[worker %s] %s\n" % (time.strftime("%H:%M:%S"), fmt % a))
    sys.stdout.flush()


class Worker:
    def __init__(self, st, wid, callback):
        self.st = st
        self.wid = wid
        self.callback = callback
        self.stop = threading.Event()
        self.attempt = None            # {job, slot, run_id, claim_token}
        self.attempt_lock = threading.Lock()

    # -- maintenance thread ------------------------------------------------
    def maintain(self, control):
        while not self.stop.is_set():
            msg = control.recv(timeout=CONFIG["heartbeat_s"])
            if msg:
                kind = msg.get("type")
                if kind == "STOP":
                    self.stop.set()
                    return
                if kind == "COMPLETE":
                    self.on_complete(control, msg)
            with self.attempt_lock:
                attempt = dict(self.attempt) if self.attempt else None
            if attempt:
                self.maintain_attempt(attempt)

    def maintain_attempt(self, attempt):
        try:
            executions.renew(self.st, attempt["job"], attempt["run_id"],
                             attempt["claim_token"], self.wid)
        except ClientError as err:
            if cond_failed(err):
                log("claim no longer mine: job=%s run_id=%s",
                    attempt["job"], attempt["run_id"])
                return
            raise

    # -- completion --------------------------------------------------------
    def on_complete(self, control, msg):
        job = msg.get("job")
        run_id = msg.get("run_id")
        token = msg.get("claim_token")
        result = msg.get("result", "ok")
        accepted = self.complete(job, run_id, token, result)
        control.send(type="COMPLETE_RESULT", job=job, run_id=run_id,
                     accepted=accepted)

    def complete(self, job, run_id, token, result):
        try:
            accepted = executions.complete(self.st, job, run_id, token, result)
            log("execution complete: job=%s run_id=%s accepted=%s",
                job, run_id, accepted)
            return bool(accepted)
        except ClientError as err:
            if cond_failed(err):
                log("completion not applicable: job=%s run_id=%s", job, run_id)
                return False
            raise

    # -- taking work -------------------------------------------------------
    def find_claim(self):
        for row in self.st.query("META", "JOB#"):
            job = row["job"]
            if self.wid not in (row.get("targets") or []):
                continue
            active = self.st.active(job)
            if active is None or active.get("status") != "LIVE":
                continue
            if "worker" in active:
                continue
            return active
        return None

    def take(self, active):
        try:
            executions.take(self.st, active["job"], active["run_id"], self.wid)
        except ClientError as err:
            if cond_failed(err):
                return None
            raise
        return active

    # -- the body ----------------------------------------------------------
    def run_attempt(self, active):
        job = active["job"]
        attempt = {
            "job": job,
            "slot": active["slot"],
            "run_id": active["run_id"],
            "claim_token": active["claim_token"],
        }
        with self.attempt_lock:
            self.attempt = attempt
        log("execution starting: job=%s slot=%s run_id=%s",
            job, active["slot"], active["run_id"])

        def body():
            time.sleep(CONFIG["short_body_s"])

        try:
            self.callback.run(job, active["slot"], active["run_id"],
                              active["claim_token"], os.getpid(), body)
        except Exception as err:                        # noqa: BLE001
            log("body failed: job=%s error=%s", job, err)
        finally:
            with self.attempt_lock:
                self.attempt = None
        self.complete(job, active["run_id"], active["claim_token"], "ok")

    # -- loop --------------------------------------------------------------
    def run(self, control):
        control.send(type="READY", role="worker", id=self.wid)
        thread = threading.Thread(target=self.maintain, args=(control,),
                                  daemon=True)
        thread.start()
        while not self.stop.is_set():
            active = self.find_claim()
            if active is None:
                time.sleep(CONFIG["poll_s"])
                continue
            if self.take(active) is None:
                continue
            self.run_attempt(active)
        log("agent: stop received")


def main():
    ap = argparse.ArgumentParser(description="worker process")
    ap.add_argument("--id", required=True)
    ap.add_argument("--state-table", default=STATE_TABLE_DEFAULT)
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--control-fd", type=int, required=True)
    ap.add_argument("--callback-socket", required=True)
    args = ap.parse_args()
    Worker(State(args.state_table, args.endpoint), args.id,
           Callback(args.callback_socket)).run(Control(args.control_fd))


if __name__ == "__main__":
    main()
