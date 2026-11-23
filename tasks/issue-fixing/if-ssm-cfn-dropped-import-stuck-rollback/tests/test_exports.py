"""Verifier for the cross-stack export handlers.

Each scenario deploys freshly named producer/consumer stacks on the verifier's own engine, always
producer-first as the app's manifest orders them (a consumer-only deploy is used where an operator
would deploy the consumer exclusively).  Expectations are derived here, from the schedule."""
import os
import subprocess

import pytest

import harness
from harness import Names, Scenario, failing_check

MISSING = None


@pytest.fixture(scope="session", autouse=True)
def environment():
    subprocess.run(["pkill", "-9", "-u", "cfnhandler"], check=False, capture_output=True)
    try:
        harness.read_param(harness.ssm_client(), "/verifier/preflight/does-not-exist")
        harness.reset_table()
    except Exception as exc:  # noqa: BLE001
        os.makedirs(harness.LOG_DIR, exist_ok=True)
        with open(os.path.join(harness.LOG_DIR, "environment_error"), "w") as fh:
            fh.write(f"emulator not usable: {exc!r}\n")
        pytest.exit(f"environment: emulator not usable: {exc!r}", returncode=3)


# --------------------------------------------------------------------------- S1: the coupled schedule

@pytest.fixture(scope="session")
def s1():
    n = Names("SharedNet", "Storefront")
    x, z, y = n.param("AlbSg", "FnGetAtt"), n.param("LegacySg", "FnGetAtt"), n.param("AlbSgV2", "FnGetAtt")
    bulk = [n.param(f"LegacyQueue{i:02d}") for i in range(1, 13)]
    v = {x: n.value("sg"), z: n.value("sg"), y: n.value("sg")}
    v.update({b: n.value("url") for b in bulk})
    vx2 = n.value("sg")
    s = Scenario("s1-coupled", n)
    s.names = {"x": x, "z": z, "y": y, "bulk": bulk, "v": v, "vx2": vx2}
    everything = [x, z, y] + bulk
    v1 = [x, z] + bulk
    v2 = [x, y]

    def steps(s):
        s.ssm.put_parameter(Name=x, Value="stale", Type="String", Overwrite=True)   # leftover, no tags
        s.deploy(n.producer, n.producer_template({k: v[k] for k in v1}))
        s.deploy(n.consumer, n.consumer_template(v1))
        s.checkpoint("CP1", everything)
        s.deploy(n.producer, n.producer_template(
            {k: v[k] for k in v2}, tail_check=failing_check("post-deploy check failed: target group unhealthy")))
        s.checkpoint("CP2", everything)
        s.deploy(n.producer, n.producer_template({k: v[k] for k in v2}))
        s.checkpoint("CP3", everything)
        s.deploy(n.consumer, n.consumer_template(v2, health_check=failing_check("health check failed: 503")))
        s.checkpoint("CP4", everything)
        s.deploy(n.consumer, n.consumer_template(v2))
        s.checkpoint("CP5", everything)
        s.deploy(n.producer, n.producer_template({x: vx2, y: v[y]}))
        s.checkpoint("CP6", everything)

    return s.run(steps)


def _cp(s, label):
    assert s.error is None, s.describe(label)
    assert label in s.checkpoints, s.describe(label)
    return s.checkpoints[label]


def test_s1_create_over_leftover_parameter(s1):
    cp, nm = _cp(s1, "CP1"), s1.names
    assert cp["producer"] == "CREATE_COMPLETE" and cp["consumer"] == "CREATE_COMPLETE", s1.describe("CP1")
    expected = {k: nm["v"][k] for k in [nm["x"], nm["z"]] + nm["bulk"]}
    assert {k: cp["params"][k] for k in expected} == expected, s1.describe("CP1")


def test_s1_failed_producer_update_rolls_back(s1):
    cp, nm = _cp(s1, "CP2"), s1.names
    assert cp["producer"] == "UPDATE_ROLLBACK_COMPLETE", s1.describe("CP2")
    for k in [nm["x"], nm["z"]] + nm["bulk"]:
        assert cp["params"][k] == nm["v"][k], s1.describe("CP2")
    assert cp["params"][nm["y"]] is MISSING, "export created by the rolled-back update is left behind\n" + s1.describe("CP2")


def test_s1_producer_drops_exports_consumer_still_uses(s1):
    cp, nm = _cp(s1, "CP3"), s1.names
    assert cp["producer"] == "UPDATE_COMPLETE", s1.describe("CP3")
    for k in [nm["x"], nm["z"], nm["y"]] + nm["bulk"]:
        assert cp["params"][k] == nm["v"][k], s1.describe("CP3")


def test_s1_failed_consumer_update_rolls_back(s1):
    cp, nm = _cp(s1, "CP4"), s1.names
    assert cp["consumer"] == "UPDATE_ROLLBACK_COMPLETE", s1.describe("CP4")
    for k in [nm["x"], nm["z"], nm["y"]] + nm["bulk"]:
        assert cp["params"][k] == nm["v"][k], s1.describe("CP4")


def test_s1_consumer_update_reclaims_dropped_exports(s1):
    cp, nm = _cp(s1, "CP5"), s1.names
    assert cp["consumer"] == "UPDATE_COMPLETE", s1.describe("CP5")
    assert cp["params"][nm["x"]] == nm["v"][nm["x"]] and cp["params"][nm["y"]] == nm["v"][nm["y"]], s1.describe("CP5")
    leaked = [k for k in [nm["z"]] + nm["bulk"] if cp["params"][k] is not MISSING]
    assert not leaked, f"{len(leaked)} dropped exports still in Parameter Store\n" + s1.describe("CP5")


def test_s1_value_change_of_used_export_refused(s1):
    cp, nm = _cp(s1, "CP6"), s1.names
    assert cp["producer"] == "UPDATE_ROLLBACK_COMPLETE", s1.describe("CP6")
    assert cp["params"][nm["x"]] == nm["v"][nm["x"]], s1.describe("CP6")
    assert cp["params"][nm["y"]] == nm["v"][nm["y"]], s1.describe("CP6")


def test_s1_consumer_imports_always_resolvable(s1):
    assert s1.error is None, s1.describe()
    assert s1.probe.checks > 0, s1.describe()
    assert not s1.probe.violations, f"{len(s1.probe.violations)} strong-reference violations, first: " \
        f"{s1.probe.violations[:3]}\n" + s1.describe()


# --------------------------------------------------------------------------- S2: consumer deployed first

@pytest.fixture(scope="session")
def s2():
    n = Names("Platform", "Checkout")
    p, q, r = n.param("Vpc"), n.param("PaymentsQueue"), n.param("AuditTopic")
    v = {p: n.value("sg"), q: n.value("url"), r: n.value("arn")}
    s = Scenario("s2-consumer-first", n)
    s.names = {"p": p, "q": q, "r": r, "v": v}

    def steps(s):
        s.deploy(n.producer, n.producer_template(v))
        s.deploy(n.consumer, n.consumer_template([p, q, r]))
        s.checkpoint("created", [p, q, r])
        s.deploy(n.consumer, n.consumer_template([p]))
        s.checkpoint("consumer-dropped", [p, q, r])
        s.deploy(n.producer, n.producer_template({p: v[p]}))
        s.checkpoint("producer-dropped", [p, q, r])

    return s.run(steps)


def test_s2_consumer_first_then_producer_drop(s2):
    nm = s2.names
    cp = _cp(s2, "consumer-dropped")
    assert cp["consumer"] == "UPDATE_COMPLETE" and cp["params"][nm["p"]] == nm["v"][nm["p"]], s2.describe("consumer-dropped")
    assert all(cp["params"][nm[k]] == nm["v"][nm[k]] for k in "qr"), \
        "exports the producer still exports were deleted\n" + s2.describe("consumer-dropped")
    cp = _cp(s2, "producer-dropped")
    assert cp["producer"] == "UPDATE_COMPLETE", s2.describe("producer-dropped")
    assert cp["params"][nm["p"]] == nm["v"][nm["p"]], s2.describe("producer-dropped")
    assert cp["params"][nm["q"]] is MISSING and cp["params"][nm["r"]] is MISSING, s2.describe("producer-dropped")
    assert not s2.probe.violations, f"strong-reference violations: {s2.probe.violations[:3]}\n" + s2.describe()


# --------------------------------------------------------------------------- S3: swap, no faults

@pytest.fixture(scope="session")
def s3():
    n = Names("Edge", "Catalog")
    k, z, y = n.param("Certificate"), n.param("CdnSg", "FnGetAtt"), n.param("CdnSgV2", "FnGetAtt")
    extra = [n.param(f"Bucket{i}") for i in range(1, 4)]
    v = {k: n.value("arn"), z: n.value("sg"), y: n.value("sg")}
    v.update({e: n.value("arn") for e in extra})
    s = Scenario("s3-swap", n)
    s.names = {"k": k, "z": z, "y": y, "extra": extra, "v": v}
    everything = [k, z, y] + extra

    def steps(s):
        s.deploy(n.producer, n.producer_template({e: v[e] for e in [k, z] + extra}))
        s.deploy(n.consumer, n.consumer_template([k, z] + extra))
        s.checkpoint("created", everything)
        s.deploy(n.producer, n.producer_template({k: v[k], y: v[y]}))
        s.checkpoint("producer", everything)
        s.deploy(n.consumer, n.consumer_template([k, y]))
        s.checkpoint("consumer", everything)

    return s.run(steps)


def test_s3_swap_import_in_one_change(s3):
    nm = s3.names
    cp = _cp(s3, "producer")
    assert cp["producer"] == "UPDATE_COMPLETE", s3.describe("producer")
    cp = _cp(s3, "consumer")
    assert cp["consumer"] == "UPDATE_COMPLETE", s3.describe("consumer")
    assert cp["params"][nm["k"]] == nm["v"][nm["k"]] and cp["params"][nm["y"]] == nm["v"][nm["y"]], s3.describe("consumer")
    leaked = [e for e in [nm["z"]] + nm["extra"] if cp["params"][e] is not MISSING]
    assert not leaked, f"dropped exports left behind: {leaked}\n" + s3.describe("consumer")
    assert not s3.probe.violations, f"strong-reference violations: {s3.probe.violations[:3]}\n" + s3.describe()


# --------------------------------------------------------------------------- S4: value change refused

@pytest.fixture(scope="session")
def s4():
    n = Names("Data", "Reports")
    a, w, u = n.param("WarehouseSg", "FnGetAtt"), n.param("ExportBucket"), n.param("ArchiveBucket")
    v = {a: n.value("sg"), w: n.value("arn"), u: n.value("arn")}
    a2 = n.value("sg")
    s = Scenario("s4-value-change", n)
    s.names = {"a": a, "w": w, "u": u, "v": v, "a2": a2}

    def steps(s):
        s.deploy(n.producer, n.producer_template({a: v[a], w: v[w]}))
        s.deploy(n.consumer, n.consumer_template([a, w]))
        s.deploy(n.producer, n.producer_template({a: a2, w: v[w]}))
        s.checkpoint("refused", [a, w, u])
        s.deploy(n.producer, n.producer_template({a: v[a], w: v[w], u: v[u]}))
        s.checkpoint("after", [a, w, u])

    return s.run(steps)


def test_s4_value_change_refused_and_stack_stays_usable(s4):
    nm = s4.names
    cp = _cp(s4, "refused")
    assert cp["producer"] == "UPDATE_ROLLBACK_COMPLETE", s4.describe("refused")
    assert cp["params"][nm["a"]] == nm["v"][nm["a"]] and cp["params"][nm["w"]] == nm["v"][nm["w"]], s4.describe("refused")
    cp = _cp(s4, "after")
    assert cp["producer"] == "UPDATE_COMPLETE", s4.describe("after")
    assert cp["params"][nm["u"]] == nm["v"][nm["u"]] and cp["params"][nm["a"]] == nm["v"][nm["a"]], s4.describe("after")
    assert not s4.probe.violations, f"strong-reference violations: {s4.probe.violations[:3]}\n" + s4.describe()


# --------------------------------------------------------------------------- S5: taking over a deployed environment

@pytest.fixture(scope="session")
def s5():
    n = Names("SharedNetwork", "Storefront")
    x, z, y = n.param("AlbSg", "FnGetAtt"), n.param("LegacyAlbSg", "FnGetAtt"), n.param("AlbSgV2", "FnGetAtt")
    bulk = [n.param(f"LegacyOrders{i:02d}") for i in range(1, 12)]
    v = {x: n.value("sg"), z: n.value("sg"), y: n.value("sg")}
    v.update({b: n.value("url") for b in bulk})
    vx2 = n.value("sg")
    s = Scenario("s5-takeover", n)
    s.names = {"x": x, "z": z, "y": y, "bulk": bulk, "v": v, "vx2": vx2}
    everything = [x, z, y] + bulk
    v1 = [x, z] + bulk
    v2 = [x, y]

    def steps(s):
        s.handlers(harness.LEGACY_APP_DIR)
        s.deploy(n.producer, n.producer_template({k: v[k] for k in v1}))
        s.deploy(n.consumer, n.consumer_template(v1))
        s.checkpoint("deployed", everything)
        s.handlers(harness.APP_DIR)
        s.deploy(n.producer, n.producer_template({**{k: v[k] for k in v1}, x: vx2}))
        s.checkpoint("T1", everything)
        s.deploy(n.producer, n.producer_template(
            {k: v[k] for k in v2}, tail_check=failing_check("post-deploy check failed: target group unhealthy")))
        s.checkpoint("T2", everything)
        s.deploy(n.producer, n.producer_template({k: v[k] for k in v2}))
        s.checkpoint("T3", everything)
        s.deploy(n.consumer, n.consumer_template(v2, health_check=failing_check("health check failed: 503")))
        s.checkpoint("T4", everything)
        s.deploy(n.producer, n.producer_template({x: vx2, y: v[y]}))
        s.checkpoint("T4b", everything)
        s.deploy(n.consumer, n.consumer_template(v2))
        s.checkpoint("T5", everything)
        s.deploy(n.producer, n.producer_template({x: vx2, y: v[y]}))
        s.checkpoint("T6", everything)
        s.deploy(n.producer, n.producer_template({x: v[x]}))
        s.checkpoint("T7", everything)
        s.deploy(n.consumer, n.consumer_template([x]))
        s.checkpoint("T8", everything)

    return s.run(steps)


def test_s5_takeover_producer_updates(s5):
    nm = s5.names
    held = [nm["x"], nm["z"]] + nm["bulk"]
    cp = _cp(s5, "deployed")
    assert cp["producer"] == "CREATE_COMPLETE" and cp["consumer"] == "CREATE_COMPLETE", s5.describe("deployed")
    cp = _cp(s5, "T1")
    assert cp["producer"] == "UPDATE_ROLLBACK_COMPLETE", s5.describe("T1")
    assert all(cp["params"][k] == nm["v"][k] for k in held), s5.describe("T1")
    cp = _cp(s5, "T2")
    assert cp["producer"] == "UPDATE_ROLLBACK_COMPLETE", s5.describe("T2")
    assert all(cp["params"][k] == nm["v"][k] for k in held), s5.describe("T2")
    assert cp["params"][nm["y"]] is MISSING, "export created by the rolled-back update is left behind\n" + s5.describe("T2")
    cp = _cp(s5, "T3")
    assert cp["producer"] == "UPDATE_COMPLETE", s5.describe("T3")
    assert all(cp["params"][k] == nm["v"][k] for k in held + [nm["y"]]), s5.describe("T3")


def test_s5_takeover_consumer_updates(s5):
    nm = s5.names
    cp = _cp(s5, "T4")
    assert cp["consumer"] == "UPDATE_ROLLBACK_COMPLETE", s5.describe("T4")
    assert all(cp["params"][k] == nm["v"][k] for k in [nm["x"], nm["z"], nm["y"]] + nm["bulk"]), s5.describe("T4")
    cp = _cp(s5, "T4b")
    assert cp["producer"] == "UPDATE_ROLLBACK_COMPLETE", "value change of an export the rolled-back consumer " \
        "still uses was not refused\n" + s5.describe("T4b")
    assert cp["params"][nm["x"]] == nm["v"][nm["x"]], s5.describe("T4b")
    cp = _cp(s5, "T5")
    assert cp["consumer"] == "UPDATE_COMPLETE", s5.describe("T5")
    assert cp["params"][nm["x"]] == nm["v"][nm["x"]] and cp["params"][nm["y"]] == nm["v"][nm["y"]], s5.describe("T5")
    leaked = [k for k in [nm["z"]] + nm["bulk"] if cp["params"][k] is not MISSING]
    assert not leaked, f"{len(leaked)} dropped exports still in Parameter Store\n" + s5.describe("T5")
    cp = _cp(s5, "T6")
    assert cp["producer"] == "UPDATE_ROLLBACK_COMPLETE", s5.describe("T6")
    assert cp["params"][nm["x"]] == nm["v"][nm["x"]] and cp["params"][nm["y"]] == nm["v"][nm["y"]], s5.describe("T6")


def test_s5_takeover_later_drop_reclaimed(s5):
    nm = s5.names
    cp = _cp(s5, "T7")
    assert cp["producer"] == "UPDATE_COMPLETE", s5.describe("T7")
    assert cp["params"][nm["x"]] == nm["v"][nm["x"]] and cp["params"][nm["y"]] == nm["v"][nm["y"]], s5.describe("T7")
    cp = _cp(s5, "T8")
    assert cp["consumer"] == "UPDATE_COMPLETE", s5.describe("T8")
    assert cp["params"][nm["x"]] == nm["v"][nm["x"]], s5.describe("T8")
    assert cp["params"][nm["y"]] is MISSING, "export no stack exports or imports any more is left behind\n" \
        + s5.describe("T8")


def test_s5_takeover_keeps_strong_references(s5):
    assert s5.error is None, s5.describe()
    assert s5.probe.checks > 0, s5.describe()
    assert not s5.probe.violations, f"{len(s5.probe.violations)} strong-reference violations, first: " \
        f"{s5.probe.violations[:3]}\n" + s5.describe()


# --------------------------------------------------------------------------- S6: takeover, consumer deployed first

@pytest.fixture(scope="session")
def s6():
    n = Names("Platform", "Checkout")
    p, q, r = n.param("Vpc"), n.param("PaymentsQueue"), n.param("AuditTopic")
    v = {p: n.value("sg"), q: n.value("url"), r: n.value("arn")}
    s = Scenario("s6-takeover-consumer-first", n)
    s.names = {"p": p, "q": q, "r": r, "v": v}

    def steps(s):
        s.handlers(harness.LEGACY_APP_DIR)
        s.deploy(n.producer, n.producer_template(v))
        s.deploy(n.consumer, n.consumer_template([p, q, r]))
        s.checkpoint("deployed", [p, q, r])
        s.handlers(harness.APP_DIR)
        s.deploy(n.consumer, n.consumer_template([p]))
        s.checkpoint("consumer-dropped", [p, q, r])
        s.deploy(n.producer, n.producer_template({p: v[p]}))
        s.checkpoint("producer-dropped", [p, q, r])

    return s.run(steps)


def test_s6_takeover_consumer_first_then_producer_drop(s6):
    nm = s6.names
    cp = _cp(s6, "consumer-dropped")
    assert cp["consumer"] == "UPDATE_COMPLETE", s6.describe("consumer-dropped")
    assert all(cp["params"][nm[k]] == nm["v"][nm[k]] for k in "pqr"), \
        "exports the producer still exports were deleted\n" + s6.describe("consumer-dropped")
    cp = _cp(s6, "producer-dropped")
    assert cp["producer"] == "UPDATE_COMPLETE", s6.describe("producer-dropped")
    assert cp["params"][nm["p"]] == nm["v"][nm["p"]], s6.describe("producer-dropped")
    assert cp["params"][nm["q"]] is MISSING and cp["params"][nm["r"]] is MISSING, s6.describe("producer-dropped")
    assert not s6.probe.violations, f"strong-reference violations: {s6.probe.violations[:3]}\n" + s6.describe()


# --------------------------------------------------------------------------- all scenarios

def test_handlers_leave_nothing_running(s1, s2, s3, s4, s5, s6):
    strays = [(s.label, x) for s in (s1, s2, s3, s4, s5, s6) for x in s.probe.strays]
    assert not strays, f"processes still running after the response was read: {strays[:5]}"
