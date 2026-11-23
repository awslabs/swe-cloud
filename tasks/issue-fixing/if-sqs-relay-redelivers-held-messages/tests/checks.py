"""Graded properties, computed from the instruments' records only (all times monotonic)."""

import math

VISIBILITY_TIMEOUT = 20
RECOVERY_TOLERANCE = 3.0     # Floci expiry + probe wait (1 s) + scheduling
STOP_GRACE = 1.0             # an extension already on its way when the signal lands
MIN_SPACING = 5.0            # README: no extension within the first quarter of a window
SPACING_SLACK = 0.2


def _fmt(t, t0):
    return f"{t - t0:.1f}s" if t is not None else "never"


class Evidence:

    def __init__(self, case):
        self.case = case
        self.t0 = case.t0
        self.calls = [rec for proxy in case.proxies.values() for rec in proxy.snapshot()]
        self.routes = case.downstream.snapshot()
        self.sightings = case.probe.snapshot()
        self.relays = case.relays
        self.mid_to_job = {mid: job for job, mid in case.jobs.items()}
        self.handle_to_mid = {}
        self.deliveries = []
        for rec in sorted(self.calls, key=lambda r: r["t_req"]):
            if rec["action"] != "ReceiveMessage" or not rec["messages"]:
                continue
            for message in rec["messages"]:
                self.handle_to_mid[message["handle"]] = message["message_id"]
                self.deliveries.append({"relay": rec["proxy"], "mid": message["message_id"],
                                        "handle": message["handle"], "r": rec["t_fwd"],
                                        "handed_out": rec["t_floci"] or rec["t_fwd"], "call": rec})
        self.extensions = []
        self.deletes = []
        for rec in self.calls:
            if rec["action"] == "ChangeMessageVisibility":
                items = [(rec["handle"], rec["vt"])]
            elif rec["action"] == "ChangeMessageVisibilityBatch":
                items = [(e["handle"], e["vt"]) for e in rec["entries"]]
            elif rec["action"] == "DeleteMessage":
                self.deletes.append({"relay": rec["proxy"], "t": rec["t_req"], "handle": rec["handle"],
                                     "mid": self.handle_to_mid.get(rec["handle"])})
                continue
            elif rec["action"] == "DeleteMessageBatch":
                for entry in rec["entries"]:
                    self.deletes.append({"relay": rec["proxy"], "t": rec["t_req"], "handle": entry["handle"],
                                         "mid": self.handle_to_mid.get(entry["handle"])})
                continue
            else:
                continue
            for handle, vt in items:
                self.extensions.append({"relay": rec["proxy"], "t": rec["t_req"], "handle": handle,
                                        "mid": self.handle_to_mid.get(handle), "vt": vt})

    def rel(self, t):
        return _fmt(t, self.t0)

    def job(self, mid):
        return self.mid_to_job.get(mid, mid)

    def extensions_of(self, relay, mid, positive=True):
        return sorted((e for e in self.extensions if e["relay"] == relay and e["mid"] == mid
                       and (not positive or (e["vt"] or 0) > 0)), key=lambda e: e["t"])

    def deliveries_of(self, relay, mid):
        return sorted((d for d in self.deliveries if d["relay"] == relay and d["mid"] == mid), key=lambda d: d["r"])

    def routes_of(self, relay, mid):
        job = self.job(mid)
        return sorted((r for r in self.routes if r["relay"] == relay and r["job"] == job), key=lambda r: r["t_begin"])

    @staticmethod
    def route_end(route):
        return route["t_end"] if route["t_end"] is not None else route["t_begin"] + route["hold"]

    # -------------------------------------------------------------------------------------------

    def hold_windows(self):
        """(relay, mid, start, end): from SQS handing the message to the relay until the relay's
        downstream call for it returned, or the relay was killed, stopped without routing it, or exited."""
        windows = []
        for relay_id, relay in self.relays.items():
            mids = sorted({d["mid"] for d in self.deliveries if d["relay"] == relay_id})
            for mid in mids:
                deliveries = self.deliveries_of(relay_id, mid)
                for index, delivery in enumerate(deliveries):
                    start = delivery["r"]
                    following = deliveries[index + 1]["r"] if index + 1 < len(deliveries) else math.inf
                    ends = [following]
                    routed = [r for r in self.routes_of(relay_id, mid) if start - 0.05 <= r["t_begin"] < following]
                    if routed:
                        ends.append(self.route_end(routed[0]))
                    elif relay.t_sigterm is not None:
                        ends.append(max(start, relay.t_sigterm))
                    for t in (relay.t_sigkill, relay.t_exit):
                        if t is not None:
                            ends.append(t)
                    windows.append((relay_id, mid, start, min(ends)))
        return windows

    def check_held_not_delivered(self):
        failures = []
        for relay, mid, start, end in self.hold_windows():
            for s in self.sightings:
                if s["message_id"] == mid and s["t_req"] >= start and s["t_resp"] <= end:
                    failures.append(
                        f"{self.job(mid)} was receivable by another consumer at {self.rel(s['t_resp'])} while "
                        f"{relay} still held it (received {self.rel(start)}, held until {self.rel(end)})")
                    break
        return failures

    def check_visibility_values(self):
        failures = []
        for rec in self.calls:
            if rec["action"] == "ReceiveMessage" and rec["vt"] not in (None, VISIBILITY_TIMEOUT):
                failures.append(f"{rec['proxy']} received with VisibilityTimeout={rec['vt']} "
                                f"(the platform's timeout is {VISIBILITY_TIMEOUT})")
        for e in self.extensions:
            if e["vt"] not in (0, VISIBILITY_TIMEOUT):
                failures.append(f"{e['relay']} changed the visibility of {self.job(e['mid'])} to {e['vt']} s "
                                f"at {self.rel(e['t'])} (only {VISIBILITY_TIMEOUT}, or 0 to release)")
        return sorted(set(failures))

    def check_extension_spacing(self):
        failures = []
        for relay_id in self.relays:
            for mid in sorted({e["mid"] for e in self.extensions if e["relay"] == relay_id and e["mid"]}):
                # SQS hands a message out when it answers the receive, however late that answer reaches the relay
                anchors = [d["handed_out"] for d in self.deliveries_of(relay_id, mid)]
                previous_ext = None
                for e in self.extensions_of(relay_id, mid):
                    reference = max([a for a in anchors if a <= e["t"] + SPACING_SLACK] + (
                        [previous_ext] if previous_ext is not None else []), default=None)
                    if reference is not None and e["t"] - reference < MIN_SPACING - SPACING_SLACK:
                        what = "its previous extension" if reference == previous_ext else "SQS handed it out"
                        failures.append(f"{relay_id} extended {self.job(mid)} at {self.rel(e['t'])}, "
                                        f"{e['t'] - reference:.1f} s after {what} (budget: not within "
                                        f"{MIN_SPACING:.0f} s)")
                        break
                    previous_ext = e["t"]
        return failures

    def check_no_delete_before_route_end(self):
        failures = []
        for d in self.deletes:
            if d["mid"] is None:
                continue
            routes = [r for r in self.routes_of(d["relay"], d["mid"]) if r["status"] == 200]
            if not any(self.route_end(r) <= d["t"] + 0.05 for r in routes):
                failures.append(f"{d['relay']} deleted {self.job(d['mid'])} at {self.rel(d['t'])} before the "
                                f"downstream had accepted it")
        return failures

    def check_everything_routed_and_queue_empty(self):
        failures = []
        for job in self.case.jobs:
            if not any(r["job"] == job and r["status"] == 200 for r in self.routes):
                failures.append(f"{job} was never accepted by the downstream")
        counts = self.case.queue_counts()
        if counts != (0, 0, 0):
            failures.append(f"queue not empty at the end (visible, in flight, delayed) = {counts}")
        return failures

    def check_poll_kept_shape(self, relay_id):
        held = [rec for rec in self.calls if rec["proxy"] == relay_id and rec["action"] == "ReceiveMessage"
                and rec["held_until"] is not None and rec["held_until"] - (rec["t_floci"] or 0) >= 10]
        if held:
            return []
        return [f"{relay_id} had no other receive request outstanding when its first messages came back: the "
                f"documented poll (MAX_MESSAGES_PER_POLL=20, two parallel receives) was not kept, so the slow "
                f"receive this case depends on never happened"]

    def standard_checks(self):
        return (self.check_held_not_delivered() + self.check_visibility_values() + self.check_extension_spacing()
                + self.check_no_delete_before_route_end())

    # -------------------------------------------------------------------------------------------

    def check_recovery_after_kill(self, relay_id, mids):
        relay = self.relays[relay_id]
        t_kill = relay.t_sigkill
        failures = []
        for mid in mids:
            deliveries = [d for d in self.deliveries_of(relay_id, mid) if d["r"] <= t_kill]
            if not deliveries:
                failures.append(f"{self.job(mid)} was never received by {relay_id} before the kill")
                continue
            if any(r["status"] == 200 and self.route_end(r) <= t_kill for r in self.routes_of(relay_id, mid)):
                continue
            last = max([deliveries[-1]["r"]] + [e["t"] for e in self.extensions_of(relay_id, mid) if e["t"] <= t_kill])
            bound = last + VISIBILITY_TIMEOUT + RECOVERY_TOLERANCE
            seen = [s["t_resp"] for s in self.sightings if s["message_id"] == mid and t_kill <= s["t_resp"]]
            first = min(seen) if seen else None
            if first is None or first > bound:
                failures.append(f"after {relay_id} was killed at {self.rel(t_kill)}, {self.job(mid)} (last visibility "
                                f"change {self.rel(last)}) was receivable again only at {self.rel(first)}, expected "
                                f"by {self.rel(bound)}")
        return failures

    def check_stop(self, relay_id, mids, exit_bound=15.0):
        relay = self.relays[relay_id]
        t_term = relay.t_sigterm
        failures = []
        in_hand = set()
        for mid in mids:
            for r in self.routes_of(relay_id, mid):
                if r["t_begin"] < t_term and self.route_end(r) > t_term:
                    in_hand.add(mid)
        for r in self.routes:
            if r["relay"] == relay_id and r["t_begin"] > t_term + 0.05:
                failures.append(f"{relay_id} handed {r['job']} downstream at {self.rel(r['t_begin'])}, after its "
                                f"SIGTERM at {self.rel(t_term)}")
        for mid in mids:
            deliveries = self.deliveries_of(relay_id, mid)
            if not deliveries or mid in in_hand:
                continue
            if any(r["status"] == 200 and self.route_end(r) <= t_term for r in self.routes_of(relay_id, mid)):
                continue
            exts = self.extensions_of(relay_id, mid)
            late = [e for e in exts if e["t"] > t_term + STOP_GRACE]
            if late:
                failures.append(f"{relay_id} extended {self.job(mid)} at {self.rel(late[0]['t'])}, after its SIGTERM "
                                f"at {self.rel(t_term)}, although it was not going to hand it downstream")
            last = max([deliveries[0]["r"]] + [e["t"] for e in exts if e["t"] <= t_term + STOP_GRACE])
            bound = last + VISIBILITY_TIMEOUT + RECOVERY_TOLERANCE
            seen = [s["t_resp"] for s in self.sightings if s["message_id"] == mid and s["t_req"] >= deliveries[0]["r"]]
            first = min(seen) if seen else None
            if first is None or first > bound:
                failures.append(f"{self.job(mid)}, left behind by the stopping {relay_id}, was receivable again only "
                                f"at {self.rel(first)}, expected by {self.rel(bound)}")
        returned = [rec["t_fwd"] for rec in self.calls if rec["proxy"] == relay_id
                    and rec["action"] == "ReceiveMessage" and rec["t_req"] < t_term and rec["t_fwd"] is not None]
        finished = [self.route_end(r) for mid in in_hand for r in self.routes_of(relay_id, mid)
                    if r["t_begin"] < t_term]
        deadline = max(returned + finished + [t_term]) + exit_bound
        if relay.t_exit is None or relay.t_exit > deadline:
            failures.append(f"{relay_id} did not exit by {self.rel(deadline)} after its SIGTERM at {self.rel(t_term)} "
                            f"(exited {self.rel(relay.t_exit)})")
        return failures, in_hand
