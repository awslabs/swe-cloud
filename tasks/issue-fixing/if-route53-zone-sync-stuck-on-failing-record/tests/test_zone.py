"""Independent verifier for the reconciler run.

Drives the run first, then asserts. Every assertion records its own result, so
one failure hides none of the others.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness as H

REWARD = "/logs/verifier/reward.txt"
RUN = {}


def parse_claim(raw):
    if raw is None:
        return None
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    labels = {}
    for part in text.split(","):
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        labels[key.strip()] = value.strip()
    if labels.get("heritage") != "external-dns":
        return None
    return labels


def data_name(name):
    if name.startswith("a-"):
        head, _, rest = name.partition(".")
        if rest:
            return head[2:] + "." + rest
    return name


def claim_of(zone, name):
    return zone.get((H.claim_name(name).rstrip("."), "TXT"))


def owner_of(zone, name):
    claim = claim_of(zone, name)
    if claim is None:
        return None
    stored = H.values_of(claim)
    labels = parse_claim(stored[0]) if stored else None
    return labels.get("external-dns/owner") if labels else None


def converged(zone, name, endpoint):
    """True when the name serves what the endpoint list asks of it, under us."""
    rs = zone.get((name, "A"))
    if rs is None or H.values_of(rs) != [endpoint["targets"][0]]:
        return False
    if rs.get("TTL") != endpoint.get("ttl", H.RECORD_TTL):
        return False
    return owner_of(zone, name) == H.OWNER_ID


# --------------------------------------------------------------- the drive


def drive():
    run = {"seed": int(time.time() * 1000) & 0x7FFFFFFF, "roles": None, "cycles": [], "log": [],
           "snapshots": {}}

    try:
        H.direct_client().list_hosted_zones(MaxItems="1")
    except Exception as exc:
        H.note("environment", "emulator reachable", False, repr(exc))
        return run
    H.note("environment", "emulator reachable", True)

    if not os.path.exists(H.RECONCILER):
        H.note("run", "reconciler present", False, "%s is missing" % H.RECONCILER)
        return run
    H.note("run", "reconciler present", True)

    if not H.restart_relay():
        H.note("environment", "relay reachable", False,
               "relay on port %d did not come up" % H.RELAY_PORT)
        return run
    H.note("environment", "relay reachable", True)

    run["roles"] = H.roles(run["seed"])
    run["zone_id"] = H.create_zone()
    run["seeds"] = H.seed_zone(run["zone_id"], run["roles"])
    run["desired"] = H.desired_endpoints(run["seed"])

    desired_path = "/tmp/desired-%s.json" % run["zone_id"]
    with open(desired_path, "w") as handle:
        json.dump(run["desired"], handle)

    H.arm(run["seed"])
    H.set_cycle(0)
    run["snapshots"][0] = H.read_zone(run["zone_id"])
    run["cycles"] = H.run_cycles(run["zone_id"], desired_path)
    for outcome in run["cycles"]:
        if outcome["cycle"] in (H.CONVERGE_BY_CYCLE, H.CYCLES):
            run["snapshots"][outcome["cycle"]] = H.read_zone(run["zone_id"])
    run["log"] = H.relay_log()
    return run


def check(run):
    roles = run["roles"]
    if roles is None:
        return
    wanted = {e["name"].rstrip("."): e for e in run["desired"]}
    snapshot = run["snapshots"].get(H.CONVERGE_BY_CYCLE, {})
    final = run["snapshots"].get(H.CYCLES, {})

    # ---- liveness by the deadline
    plain = [n.rstrip(".") for n in roles["plain"]]
    for name in plain:
        target = wanted[name]["targets"][0]
        rs = snapshot.get((name, "A"))
        claim = claim_of(snapshot, name)
        ok = (
            rs is not None
            and H.values_of(rs) == [target]
            and rs.get("TTL") == H.RECORD_TTL
            and claim is not None
            and H.values_of(claim) == [H.claim_value(H.OWNER_ID)]
        )
        H.note("liveness-by-cycle-%d" % H.CONVERGE_BY_CYCLE, "%s converged" % name, ok,
               "A=%r claim=%r" % (H.values_of(rs) if rs else None,
                                  H.values_of(claim) if claim else None))

    rname = roles["restore"].rstrip(".")
    rrs = snapshot.get((rname, "A"))
    rclaim = claim_of(snapshot, rname)
    H.note("liveness-by-cycle-%d" % H.CONVERGE_BY_CYCLE, "record restored under its claim",
           rrs is not None
           and H.values_of(rrs) == [wanted[rname]["targets"][0]]
           and rclaim is not None
           and H.values_of(rclaim) == [run["seeds"]["restore_claim"]],
           "A=%r claim=%r" % (H.values_of(rrs) if rrs else None,
                              H.values_of(rclaim) if rclaim else None))

    sname = roles["stale"].rstrip(".")
    srs = snapshot.get((sname, "A"))
    sclaim = claim_of(snapshot, sname)
    H.note("liveness-by-cycle-%d" % H.CONVERGE_BY_CYCLE, "owned record with a stale value corrected",
           srs is not None
           and H.values_of(srs) == [wanted[sname]["targets"][0]]
           and srs.get("TTL") == H.RECORD_TTL
           and sclaim is not None
           and H.values_of(sclaim) == [run["seeds"]["stale_claim"]],
           "A=%r claim=%r" % (H.values_of(srs) if srs else None,
                              H.values_of(sclaim) if sclaim else None))

    # ---- ownership safety, read off the final zone
    safety = [
        ("other owner's claimed name left alone", roles["claimed"].rstrip("."),
         lambda n: final.get((n, "A")) is None
         and H.values_of(claim_of(final, n)) == [run["seeds"]["claimed_claim"]]),
        ("unclaimed foreign record left alone", roles["taken"].rstrip("."),
         lambda n: H.values_of(final.get((n, "A"))) == [H.TAKEN_VALUE]
         and claim_of(final, n) is None),
        ("once-occupied name left alone", roles["race"].rstrip("."),
         lambda n: H.values_of(final.get((n, "A"))) == [H.RACE_VALUE]
         and claim_of(final, n) is None),
        ("repeatedly-occupied name left alone", roles["flap"].rstrip("."),
         lambda n: final.get((n, "A")) is None and claim_of(final, n) is None),
        ("retired pair removed", H.GONE_NAME.rstrip("."),
         lambda n: final.get((n, "A")) is None and claim_of(final, n) is None),
        ("other controller's pair untouched", H.THEIRS_NAME.rstrip("."),
         lambda n: H.values_of(final.get((n, "A"))) == ["192.0.2.20"]
         and H.values_of(claim_of(final, n)) == [run["seeds"]["theirs_claim"]]),
    ]
    for label, name, holds in safety:
        H.note("ownership-safety", "%s (%s)" % (label, name), holds(name),
               "A=%r claim=%r" % (H.values_of(final.get((name, "A"))),
                                  H.values_of(claim_of(final, name))))

    orphans = []
    for (name, rtype) in final:
        if rtype != "TXT":
            continue
        stored = H.values_of(final[(name, rtype)])
        labels = parse_claim(stored[0]) if stored else None
        if labels is None or labels.get("external-dns/owner") != H.OWNER_ID:
            continue
        if final.get((data_name(name), "A")) is None:
            orphans.append(name)
    H.note("ownership-safety", "no claim of ours without its record", not orphans,
           "orphans=%r" % orphans)

    expected = set()
    for name in plain:
        expected.add((name, "A"))
        expected.add((H.claim_name(name).rstrip("."), "TXT"))
    expected.add((rname, "A"))
    expected.add((H.claim_name(rname).rstrip("."), "TXT"))
    expected.add((sname, "A"))
    expected.add((H.claim_name(sname).rstrip("."), "TXT"))
    expected.add((H.claim_name(roles["claimed"]).rstrip("."), "TXT"))
    expected.add((roles["taken"].rstrip("."), "A"))
    expected.add((roles["race"].rstrip("."), "A"))
    expected.add((H.THEIRS_NAME.rstrip("."), "A"))
    expected.add((H.claim_name(H.THEIRS_NAME).rstrip("."), "TXT"))
    zone_apex = (H.ZONE_NAME.rstrip("."), "SOA"), (H.ZONE_NAME.rstrip("."), "NS")
    present = {key for key in final if key not in zone_apex}
    H.note("zone-contents", "zone holds exactly the expected records", present == expected,
           "unexpected=%r missing=%r" % (sorted(present - expected),
                                         sorted(expected - present)))

    # ---- the calls themselves
    accepted = [c for c in run["log"] if c.get("accepted")]
    for cycle in range(1, H.CYCLES + 1):
        count = len([c for c in run["log"] if c.get("cycle") == cycle])
        H.note("call-log", "cycle %d made %d calls" % (cycle, count),
               1 <= count <= H.MAX_CALLS_PER_CYCLE)

    for index, call in enumerate(accepted):
        a_names = {data_name(i["name"]) for i in call["items"] if i["type"] == "A"}
        t_names = {data_name(i["name"]) for i in call["items"] if i["type"] == "TXT"}
        H.note("call-log", "accepted call %d keeps every claim with its record" % index,
               t_names <= a_names, "claims-without-record=%r" % sorted(t_names - a_names))
        for item in call["items"]:
            if item["type"] != "A" or item["action"] not in ("CREATE", "DELETE"):
                continue
            if data_name(item["name"]) == rname:
                continue
            H.note("call-log", "accepted call %d carries %s of %s with its claim"
                   % (index, item["action"], item["name"]),
                   data_name(item["name"]) in t_names)

    guarded = {
        H.claim_name(roles["claimed"]).rstrip("."),
        H.claim_name(H.THEIRS_NAME).rstrip("."),
        roles["taken"].rstrip("."),
        H.THEIRS_NAME.rstrip("."),
    }
    touched = sorted({i["name"] for c in accepted for i in c["items"] if i["name"] in guarded})
    H.note("call-log", "no pre-existing claim or foreign record was rewritten",
           not touched, "touched=%r" % touched)

    seen_a, seen_txt = set(), set()
    for call in accepted:
        for item in call["items"]:
            if item["type"] == "A":
                seen_a.add(data_name(item["name"]))
            else:
                seen_txt.add(data_name(item["name"]))
    missing = sorted(n for n in plain + [rname, sname] if n not in seen_a)
    H.note("call-log", "every record of ours went through the relay", not missing,
           "never-relayed=%r" % missing)
    missing_claims = sorted(n for n in plain if n not in seen_txt)
    H.note("call-log", "every claim of ours went through the relay", not missing_claims,
           "never-relayed=%r" % missing_claims)

    # ---- the report
    last = run["cycles"][-1] if run["cycles"] else None
    if last is None:
        H.note("report", "cycles ran", False, "no cycle outcome recorded")
        return
    report = H.report_of(last)
    if report is None:
        H.note("report", "last cycle printed one JSON report line", False,
               repr((last.get("stdout") or "")[-400:]))
    else:
        outstanding = sorted(n for n, e in wanted.items() if not converged(final, n, e))
        missing = [n for n in outstanding if n not in report["failed"]]
        H.note("report", "last cycle reports every endpoint still not in shape",
               not missing, "not-in-shape=%r missing=%r failed=%r"
               % (outstanding, missing, report["failed"]))
        settled = sorted(n for n, e in wanted.items()
                         if converged(final, n, e) and n in report["failed"])
        H.note("report", "last cycle reports no in-shape name as failed", not settled,
               "failed=%r" % report["failed"])
    for outcome in run["cycles"]:
        H.note("report", "cycle %d exited %r" % (outcome["cycle"], outcome["rc"]),
               outcome["rc"] in (0, 2) and not outcome["timed_out"],
               (outcome.get("stderr") or "")[-300:])


try:
    RUN.update(drive())
    check(RUN)
finally:
    os.makedirs("/logs/verifier", exist_ok=True)
    outcome = 1 if (H.results() and all(r["ok"] for r in H.results())) else 0
    with open(REWARD, "w") as handle:
        handle.write("%d\n" % outcome)
    with open("/logs/verifier/detail.txt", "w") as handle:
        for record in H.results():
            handle.write("%-4s %-24s %-52s %s\n" % (
                "ok" if record["ok"] else "FAIL", record["group"], record["name"],
                record["detail"]))


def _cases(group=None):
    return [r for r in H.results() if group is None or r["group"] == group]


def test_environment():
    assert all(r["ok"] for r in _cases("environment"))


def test_run_setup():
    assert all(r["ok"] for r in _cases("run"))


def test_liveness():
    assert all(r["ok"] for r in _cases() if r["group"].startswith("liveness"))


def test_ownership_safety():
    assert all(r["ok"] for r in _cases("ownership-safety"))


def test_zone_contents():
    assert all(r["ok"] for r in _cases("zone-contents"))


def test_call_log():
    assert all(r["ok"] for r in _cases("call-log"))


def test_report():
    assert all(r["ok"] for r in _cases("report"))
