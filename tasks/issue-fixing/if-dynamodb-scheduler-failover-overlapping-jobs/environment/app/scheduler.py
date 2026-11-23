"""The scheduler process: owns the schedule while it owns the lease.

Nominals are numbered per job.  Slot 0 is the job's opening nominal and is due
at the job's start_at; slots 1..N exist once the anchor is written and are due
at anchor + k * period.  Each nominal receives exactly one terminal decision,
in order:

  RUN             admitted; an attempt is minted and a worker takes it
  SKIPPED_BUSY    a running attempt already owned the job
  MISSED_OUTAGE   no decision landed inside the window; the nominal is lost,
                  never replayed

The cursor records how far the job has been decided.  A scheduler that is not
the lease owner does nothing.
"""

import argparse
import sys
import time

import cluster
import executions
from botocore.exceptions import ClientError
from control import Control
from proto import CONFIG, STATE_TABLE_DEFAULT, State, now, cond_failed
from view import RunningView


def log(fmt, *a):
    sys.stdout.write("[scheduler %s] %s\n" % (time.strftime("%H:%M:%S"), fmt % a))
    sys.stdout.flush()


def load_jobs(st):
    specs = {}
    for row in st.query("META", "JOB#"):
        specs[row["job"]] = row
    return specs


class Scheduler:
    def __init__(self, st, sid):
        self.st = st
        self.sid = sid
        self.jobs = load_jobs(st)
        self.anchor = st.get("META", "ARM")
        self.view = RunningView(st)
        self.leading = False
        self.epoch = None

    # -- leadership --------------------------------------------------------
    def became_leader(self, epoch):
        # Attempts dispatched under a previous term keep running to completion.
        # The schedule state this process loaded when it started is kept as it
        # is, so that handover does not disturb them.
        log("agent: starting scheduler jobs=%d", len(self.jobs))
        self.leading = True
        self.epoch = epoch

    def lost_leadership(self):
        log("agent: scheduler stopped")
        self.leading = False
        self.epoch = None

    # -- deciding ----------------------------------------------------------
    def nominals(self, job, spec, anchor):
        due = [(0, spec["start_at"])]
        if anchor:
            period = spec["period_s"]
            for k in range(1, anchor["slots"] + 1):
                due.append((k, anchor["anchor"] + k * period))
        return due

    def decide(self, job, spec, anchor, t):
        cur = self.st.cursor(job)
        for slot, nominal in self.nominals(job, spec, anchor):
            if slot < cur:
                continue
            try:
                if self.st.slot(job, slot) is not None:
                    self.advance(job, cur, slot + 1)
                    cur = slot + 1
                    continue
                if t > nominal + CONFIG["decision_window_s"]:
                    self.record(job, slot, nominal, "MISSED_OUTAGE", t)
                elif t >= nominal:
                    if self.view.busy(job):
                        self.record(job, slot, nominal, "SKIPPED_BUSY", t)
                    else:
                        run_id, token = executions.claim(
                            self.st, job, slot, spec["targets"], t)
                        self.view.note_dispatch(job, run_id)
                        self.record(job, slot, nominal, "RUN", t, run_id, token)
                else:
                    return
                self.advance(job, cur, slot + 1)
                cur = slot + 1
            except ClientError as err:
                if cond_failed(err):
                    return        # another writer moved first; re-read next pass
                raise

    def record(self, job, slot, nominal, disposition, t, run_id=None, token=None):
        attrs = {
            "job": job,
            "slot": int(slot),
            "nominal_at": nominal,
            "disposition": disposition,
            "decided_at": t,
        }
        if run_id:
            attrs["run_id"] = run_id
        if token:
            attrs["claim_token"] = token
        self.st.put(self.st.job_key(job), self.st.slot_sk(slot), attrs,
                    condition="attribute_not_exists(sk)")
        log("schedule: job=%s slot=%d disposition=%s", job, slot, disposition)

    def advance(self, job, expected, new_next):
        self.st.update(self.st.job_key(job), "CURSOR", {"next_slot": int(new_next)},
                       condition="attribute_not_exists(#n) OR #n = :e",
                       names={"#n": "next_slot"},
                       values={":e": {"N": repr(expected)}})

    def tick(self, t):
        self.view.sweep()
        for job in sorted(self.jobs):
            self.decide(job, self.jobs[job], self.anchor, t)

    # -- loop --------------------------------------------------------------
    def run(self, control):
        control.send(type="READY", role="scheduler", id=self.sid, jobs=sorted(self.jobs))
        while True:
            msg = control.recv(timeout=CONFIG["poll_s"])
            if msg and msg.get("type") == "STOP":
                log("agent: stop received")
                return
            t = now()
            if not self.leading:
                epoch = cluster.try_acquire(self.st, self.sid, t)
                if epoch is not None:
                    log("cluster: lease acquired epoch=%d", epoch)
                    self.became_leader(epoch)
                continue
            if not cluster.renew(self.st, self.sid, self.epoch, t):
                self.lost_leadership()
                continue
            self.tick(t)


def main():
    ap = argparse.ArgumentParser(description="scheduler process")
    ap.add_argument("--id", required=True)
    ap.add_argument("--state-table", default=STATE_TABLE_DEFAULT)
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--control-fd", type=int, required=True)
    args = ap.parse_args()
    Scheduler(State(args.state_table, args.endpoint), args.id).run(Control(args.control_fd))


if __name__ == "__main__":
    main()
