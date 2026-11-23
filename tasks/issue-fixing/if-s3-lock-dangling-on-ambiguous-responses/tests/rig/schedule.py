"""The fault schedule applied to the faulted worker's lock writes.

The schedule fires five situations once each, in order, as the worker walks its sections. Each
models one line of the incident log:

* P1 — the worker's first acquire write applies, then the answer is lost (503). The worker must
  not strand its own fresh lease.
* P2 — the worker's first release write is refused twice (503, not applied), then allowed. The
  worker must keep retrying the same conditional write until it lands.
* P3 — the worker's second release write applies, then the connection drops. While the worker is
  unsure, the sidecar takes a fresh lease. The worker must not write over the sidecar's live lease.
* P4 — the worker's next acquire write is not applied; the sidecar instead takes a lease that
  reuses the worker's own lease end (a same-second collision), then the worker gets a 503. The
  worker must not adopt the sidecar's lease as its own.
* P5 — the worker's last release write applies, then the connection drops. The worker must
  recognise the landed release rather than report a failure.

Two more act on the ledger counter the worker writes inside its critical section:

* L2 — the counter write of the worker's second commit applies, then the answer is lost (503).
* L1 — the counter write of the worker's third commit is answered 504 at once; the gateway keeps
  the request and hands it to storage later, once the counter has moved past it (or after a bound).
  A write the worker made while it held the lock must not land over a later holder's commit.
"""
import json
import threading
import time


class Decision:
    def __init__(self, action, after_land=None, before_fail=None, after_late=None,
                 late_timeout_s=15.0):
        self.action = action
        self.after_land = after_land or (lambda handler, body, written: None)
        self.before_fail = before_fail or (lambda handler, body: None)
        self.after_late = after_late or (lambda status: None)
        self.late_timeout_s = late_timeout_s


class FaultPolicy:
    def __init__(self, rig, role, sections_w1, release_fail_count, gate_timeout_s):
        self.rig = rig
        self.role = role
        self.sections_w1 = sections_w1
        self.release_fail_count = release_fail_count
        self.gate_timeout_s = gate_timeout_s

        self.p1_done = False
        self.p2_done = False
        self.p3_done = False
        self.p4_done = False
        self.p5_done = False
        self.p2_fail_seen = 0
        self.p2_if_match = None
        self.w1_releases_landed = 0
        self.counter_seqs = []           # distinct seqs the worker's counter writes carried

    # -- helpers -------------------------------------------------------------

    def _mark(self, name):
        p = self.rig.phases.setdefault(name, {})
        p["t"] = self.rig.now()
        p["epoch"] = int(time.time() * 1000)

    def _last_etag(self):
        return self.rig.chain[-1].etag if self.rig.chain else None

    # -- the ledger counter ----------------------------------------------------

    def decide_counter(self, raw):
        try:
            seq = int(json.loads(raw).get("seq"))
        except Exception:
            return Decision("forward")
        if seq in self.counter_seqs:
            return Decision("forward")           # a retry of a commit already seen
        self.counter_seqs.append(seq)
        commit_no = len(self.counter_seqs)

        if commit_no == 2:
            def after(handler, b, up):
                self._mark("L2")
                self.rig.phases["L2"]["seq"] = seq
            return Decision("land_then_503", after_land=after)

        if commit_no == 3:
            self._mark("L1")
            self.rig.phases["L1"]["seq"] = seq

            def late(status):
                self.rig.phases["L1"]["late_status"] = status
                self.rig.phases["L1"]["late_t"] = self.rig.now()
            return Decision("504_then_late", after_late=late)
        return Decision("forward")

    # -- the decision --------------------------------------------------------

    def decide(self, if_match, body):
        is_release = bool(body.get("expired"))

        # P1: first acquire applies, then answer lost.
        if not is_release and not self.p1_done:
            def after(handler, b, written):
                if written is None:
                    return                       # lost the race to land; P1 re-fires next acquire
                self.p1_done = True
                self._mark("P1")
                self.rig.phases["P1"]["etag"] = written.etag
            return Decision("land_then_503", after_land=after)

        # P2: first release refused twice, then allowed.
        if is_release and not self.p2_done:
            if self.p2_if_match is None:
                self.p2_if_match = if_match
                self._mark("P2")
            if self.p2_fail_seen < self.release_fail_count:
                self.p2_fail_seen += 1
                return Decision("fail_503")

            def after(handler, b, written):
                self.p2_done = True
                self.w1_releases_landed += 1
            return Decision("forward", after_land=after)

        # P3: second release applies, then the sidecar takes a fresh lease, then the drop.
        if is_release and self.p2_done and not self.p3_done:
            self.rig.hold_w2()

            def after(handler, b, written):
                if written is None:
                    self.rig.release_w2()        # release did not land; P3 re-fires next release
                    return
                self.w1_releases_landed += 1
                done = threading.Event()
                try:
                    self.rig.sidecar.acquire_fresh(done)
                finally:
                    self.rig.release_w2()
                self.p3_done = True
                self._mark("P3")
            return Decision("land_then_close", after_land=after)

        # P4: next acquire not applied; the sidecar takes a colliding lease, then a 503.
        if (not is_release) and self.p3_done and not self.p4_done:
            self.rig.hold_w2()
            worker_valid_until = int(body.get("validUntil", 0))

            def before(handler, b):
                done = threading.Event()
                try:
                    self.rig.sidecar.acquire_colliding(worker_valid_until, done)
                finally:
                    self.rig.release_w2()
                self.p4_done = True
                self._mark("P4")
            return Decision("sidecar_then_503", before_fail=before)

        # P5: the last release applies, then the connection drops; gate the rival until the
        # worker has had a chance to read the lock back.
        if is_release and self.p4_done and not self.p5_done:
            if self.w1_releases_landed + 1 >= self.sections_w1:
                self.rig.hold_w2()

                def after(handler, b, written):
                    if written is None:
                        self.rig.release_w2()    # release did not land; P5 re-fires next release
                        return
                    self.w1_releases_landed += 1
                    self.p5_done = True
                    self._mark("P5")
                    self.rig.phases["P5"]["etag"] = written.etag
                    threading.Timer(self.gate_timeout_s, self.rig.release_w2).start()
                return Decision("land_then_close", after_land=after)

        # Any other write from the faulted worker: forward untouched.
        if is_release:
            def after(handler, b, written):
                self.w1_releases_landed += 1
            return Decision("forward", after_land=after)
        return Decision("forward")
