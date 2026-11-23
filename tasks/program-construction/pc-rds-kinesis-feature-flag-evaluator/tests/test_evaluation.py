"""Evaluation: constraint operators, segment order, rollout bucketing, blank results,
batch selection, the GET forms and request validation."""

import json
import urllib.parse

import pytest

from flagr_support import RANDOM_ENTITY, assert_doc, assert_timestamped, assert_unordered

# Contexts and constraints whose outcomes were observed on the reference.
CONTEXTS_A = [
    {"s": "CA", "n": 25, "f": 2.5, "b": True, "l": ["a", "b"], "u": {"name": "Alice", "age": 30}, "ns": "10", "e": "user@example.com", "nums": [1, 2]},
    {"s": "NY", "n": 18, "f": -1, "b": False, "l": ["c"], "u": {"name": "Bob", "age": 15}, "ns": "5", "e": "x@other.org", "nums": [3]},
    {},
    {"s": 5, "n": "25", "b": "true", "l": "abc", "u": "Alice", "f": None},
    {"s": "ca", "n": 18.5, "e": "USER@EXAMPLE.COM", "l": [], "nums": [1.0]},
]
CASES_A = [
    (("s", "EQ", '"CA"'), "+----"),
    (("s", "NEQ", '"CA"'), "-+--+"),
    (("s", "IN", '["CA","NY"]'), "++---"),
    (("s", "NOTIN", '["CA", "NY"]'), "----+"),
    (("s", "EREG", '"^C"'), "+----"),
    (("s", "NEREG", '"^C"'), "-+--+"),
    (("e", "EREG", '"(?i)@EXAMPLE"'), "+---+"),
    (("e", "EREG", '"a/b"'), "-----"),
    (("n", "GT", "18"), "+---+"),
    (("n", "GTE", "18"), "++--+"),
    (("n", "LT", "18"), "-----"),
    (("n", "LTE", "18"), "-+---"),
    (("n", "EQ", "25"), "+----"),
    (("n", "EQ", "25.0"), "+----"),
    (("n", "NEQ", "25"), "-+--+"),
    (("f", "LT", "0"), "-+---"),
    (("f", "EQ", "2.5"), "+----"),
    (("b", "EQ", "true"), "+----"),
    (("b", "NEQ", "true"), "-+---"),
    (("n", "IN", "[18, 25]"), "++---"),
    (("n", "NOTIN", "[18]"), "+---+"),
    (("l", "CONTAINS", '"a"'), "+----"),
    (("l", "NOTCONTAINS", '"a"'), "-+---"),
    (("s", "CONTAINS", '"A"'), "-----"),
    (("nums", "CONTAINS", "1"), "+---+"),
    (("u.name", "EQ", '"Alice"'), "+----"),
    (("u.age", "GT", "18"), "+----"),
    (("l[0]", "EQ", '"a"'), "+----"),
    (("ns", "GT", "5"), "-----"),
    (("s", "GT", '"A"'), "-----"),
    (("n", "EQ", '"25"'), "---+-"),
    (("s", "IN", '[ "CA" ]'), "+----"),
    (("s", "EQ", ' "CA" '), "+----"),
    (("missing", "EQ", '"x"'), "-----"),
    (("missing", "NEQ", '"x"'), "-----"),
    (("n", "GT", "1e1"), "++--+"),
    (("n", "LT", "-3"), "-----"),
]
CONTEXTS_B = [
    {"s": "CA", "n": 25, "f": 2.5, "b": True, "l": ["a", "b"], "u": {"name": "Alice", "age": 30, "tags": ["vip-12"]}, "e": "user@example.com", "nums": [1, 2]},
    {"s": "NY", "n": 18, "f": -1, "b": False, "l": ["c"], "u": {"name": "Bob", "age": 15, "tags": ["std"]}, "e": "x@example-com.org", "nums": [3]},
    {"s": "ca", "n": 18.5, "e": "USER@EXAMPLE.COM", "l": [], "nums": [1.0], "u": {"tags": []}},
]
CASES_B = [
    ([("e", "EREG", '"@example\\.com$"')], "+--"),
    ([("e", "NEREG", '"@example\\.com$"')], "-++"),
    ([("u.tags[0]", "EREG", '"^vip-\\d+$"')], "+--"),
    ([("e", "EREG", '"example.com"')], "++-"),
    ([("s", "EREG", '"^(CA|NY)$"')], "++-"),
    ([("s", "IN", '["ca", "NY"]')], "-++"),
    ([("n", "IN", "[18.5, 25]")], "+-+"),
    ([("f", "GTE", "2.5")], "+--"),
    ([("nums", "NOTCONTAINS", "1")], "-+-"),
    ([("l", "CONTAINS", '"c"')], "-+-"),
    ([("u.age", "LT", "20")], "-+-"),
    ([("s", "NEQ", '"ca"')], "++-"),
    ([("s", "EQ", '"CA"'), ("n", "GT", "20")], "+--"),
]


def segment_logs(result):
    """Segment debug logs; an absent or empty evalDebugLog counts as none."""
    return (result.get("evalDebugLog") or {}).get("segmentDebugLogs") or []


def run_matrix(s, cases, contexts):
    ids = []
    for i, (constraints, _) in enumerate(cases):
        f = s.flag(f"c{i}", variants=["on"])
        s.segment(f, "seg", 100, [("on", 100)], constraints)
        s.ok("PUT", f"/flags/{f['id']}/enabled", {"enabled": True})
        ids.append(f["id"])
    s.sync()
    results = s.batch({"entities": [{"entityID": f"ent{j}", "entityContext": c} for j, c in enumerate(contexts)],
                       "flagIDs": ids, "enableDebug": True})
    assert len(results) == len(ids) * len(contexts)
    by_flag = {}
    for r in results:
        by_flag.setdefault(r.get("flagID"), []).append(r)
    for i, (constraints, expected) in enumerate(cases):
        fid = ids[i]
        got = by_flag[fid]
        assert [r["evalContext"]["entityID"] for r in got] == [f"ent{j}" for j in range(len(contexts))]
        outcome = "".join("+" if r.get("variantKey") == "on" else "-" for r in got)
        assert outcome == expected, f"constraints {constraints}: got {outcome}, expected {expected}"
        for r in got:
            matched = r.get("variantKey") == "on"
            # Flag i has one variant and one segment, both with id i+1.
            assert r.get("variantID", 0) == (fid if matched else 0) and r.get("segmentID", 0) == (fid if matched else 0), r
            logs = segment_logs(r)
            assert [x.get("segmentID") for x in logs] == [fid] and all(x.get("msg") for x in logs), r


def test_constraint_operators_and_value_types(flagr):
    s = flagr(recorder=False)
    run_matrix(s, [([c], e) for c, e in CASES_A], CONTEXTS_A)


def test_constraint_patterns_lists_and_conjunction(flagr):
    s = flagr(recorder=False)
    run_matrix(s, CASES_B, CONTEXTS_B)


def test_unevaluable_constraints_fall_through_to_next_segment(flagr):
    s = flagr(recorder=False)
    f = s.flag("badre", variants=["on", "off"])
    s1 = s.segment(f, "bad regex", 100, [("on", 100)], [("s", "EREG", '"("')])
    s2 = s.segment(f, "catch all", 100, [("off", 100)])
    g = s.flag("guarded", variants=["on"])
    g1 = s.segment(g, "needs context", 100, [("on", 100)], [("s", "EQ", '"CA"')])
    for flag in (f, g):
        s.ok("PUT", f"/flags/{flag['id']}/enabled", {"enabled": True})
    s.sync()
    r = s.evaluate({"entityID": "x", "flagID": 1, "entityContext": {"s": "CA"}, "enableDebug": True})
    assert_doc(r, {"evalContext": {"entityID": "x", "flagID": 1, "entityContext": {"s": "CA"}, "enableDebug": True},
                   "flagID": 1, "flagKey": "badre", "flagSnapshotID": 14, "recordSource": "evaluation",
                   "segmentID": s2, "variantID": 2, "variantKey": "off",
                   "evalDebugLog": {"segmentDebugLogs": [{"segmentID": s1, "msg": "x"}, {"segmentID": s2, "msg": "x"}]}})
    assert_timestamped(r)
    for context in ["CA", [1, 2], 5, None, {"s": "NY"}]:
        r = s.evaluate({"entityID": "x", "flagID": 2, "entityContext": context, "enableDebug": True})
        echo = {"entityID": "x", "flagID": 2, "enableDebug": True}
        if context is not None:
            echo["entityContext"] = context
        assert_doc(r, {"evalContext": echo, "flagID": 2, "flagKey": "guarded", "flagSnapshotID": 15, "recordSource": "evaluation",
                       "evalDebugLog": {"segmentDebugLogs": [{"segmentID": g1, "msg": "x"}]}}, what=f"context {context!r}")
    # Segments without constraints accept any context.
    r = s.evaluate({"entityID": "x", "flagID": 1, "entityContext": "CA"})
    assert r.get("variantKey") == "off" and r.get("segmentID") == s2, r


SPLIT = "a c c c c c c c b b a a c c b c a c b c c c b a b c c b b c b b b c b b c c c b".split()
HALF = "- off - off on - - - off - - off on - on - on - - off off - off off - - - - off on on - - off on on on - - -".split()
ENTITIES = [f"user-{i}" for i in range(40)]


def bucketing_flags(s):
    split = s.flag("split", variants=["a", "b", "c"])
    s.segment(split, "all", 100, [("a", 20), ("b", 30), ("c", 50)])
    half = s.flag("half", variants=["on", "off"])
    s.segment(half, "all", 50, [("on", 50), ("off", 50)])
    for flag in (split, half):
        s.ok("PUT", f"/flags/{flag['id']}/enabled", {"enabled": True})
    s.ok("PUT", "/flags/2", {"entityType": "account"})
    s.sync()
    return split, half


def test_bucketing_and_partial_rollout_assignments(flagr):
    s = flagr(recorder=False)
    split, half = bucketing_flags(s)
    ids = {"a": 1, "b": 2, "c": 3, "on": 4, "off": 5}
    for attempt in range(2):
        results = s.batch({"entities": [{"entityID": e} for e in ENTITIES], "flagIDs": [1, 2]})
        assert len(results) == 80
        for i, entity in enumerate(ENTITIES):
            for r, fid, key, seg, expected in ((results[2 * i], 1, "split", 1, SPLIT[i]), (results[2 * i + 1], 2, "half", 2, HALF[i])):
                context = {"entityID": entity, "flagID": fid}
                if fid == 2:
                    context["entityType"] = "account"
                want = {"evalContext": context, "flagID": fid, "flagKey": key, "flagSnapshotID": 12 if fid == 1 else 14,
                        "recordSource": "evaluation"}
                if expected != "-":
                    want.update(segmentID=seg, variantID=ids[expected], variantKey=expected)
                assert_doc(r, want, what=f"{key} for {entity} (pass {attempt})")
    # Single evaluations agree with the batch, by id or by key.
    for i in (0, 4, 18, 33):
        one = s.evaluate({"entityID": ENTITIES[i], "flagKey": "half", "entityType": "user"})
        assert one.get("variantKey", "-") == HALF[i] and one["evalContext"]["entityType"] == "account", one
        assert s.evaluate({"entityID": ENTITIES[i], "flagID": 1}).get("variantKey") == SPLIT[i]


def test_segments_are_tried_in_rank_order_and_deletions_apply(flagr):
    s = flagr(recorder=False)
    f = s.flag("ranked", variants=["x", "y"])
    holdout = s.segment(f, "ca-holdout", 0, [("x", 100)], [("state", "EQ", '"CA"')])
    adults = s.segment(f, "adults", 100, [("y", 100)], [("age", "GTE", "21")])
    rest = s.segment(f, "rest", 100, [("x", 100)])
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    s.sync()

    def check(context, segment, variant, logged, snapshot):
        r = s.evaluate({"entityID": "e1", "flagID": 1, "entityContext": context, "enableDebug": True})
        want = {"evalContext": {"entityID": "e1", "flagID": 1, "entityContext": context, "enableDebug": True},
                "flagID": 1, "flagKey": "ranked", "flagSnapshotID": snapshot, "recordSource": "evaluation",
                "evalDebugLog": {"segmentDebugLogs": [{"segmentID": x, "msg": "x"} for x in logged]}}
        if variant:
            want.update(segmentID=segment, variantID={"x": 1, "y": 2}[variant], variantKey=variant)
        assert_doc(r, want, what=f"context {context}")

    # A matching segment with 0% rollout stops evaluation without a variant.
    check({"state": "CA", "age": 30}, None, None, [holdout], 12)
    check({"state": "CA"}, None, None, [holdout], 12)
    check({"state": "NY", "age": 30}, adults, "y", [holdout, adults], 12)
    check({"state": "NY", "age": 18}, rest, "x", [holdout, adults, rest], 12)
    check({}, rest, "x", [holdout, adults, rest], 12)
    s.ok("PUT", "/flags/1/segments/reorder", {"segmentIDs": [adults, holdout, rest]})
    s.sync()
    check({"state": "CA", "age": 30}, adults, "y", [adults], 14)
    check({"state": "CA", "age": 18}, None, None, [adults, holdout], 14)
    check({"state": "TX", "age": 18}, rest, "x", [adults, holdout, rest], 14)
    # Deleted constraints and segments drop out of evaluation once the view refreshes.
    s.ok("DELETE", f"/flags/1/segments/{adults}/constraints/2")
    s.sync()
    check({"state": "CA", "age": 18}, adults, "y", [adults], 16)
    check({"state": "TX"}, adults, "y", [adults], 16)
    s.ok("DELETE", f"/flags/1/segments/{adults}")
    s.sync()
    check({"state": "CA", "age": 18}, None, None, [holdout], 18)
    check({"state": "TX", "age": 40}, rest, "x", [holdout, rest], 18)
    check({}, rest, "x", [holdout, rest], 18)
    s.ok("DELETE", f"/flags/1/segments/{holdout}/constraints/1")
    s.sync()
    check({"state": "TX"}, None, None, [holdout], 20)


def test_blank_results(flagr):
    s = flagr(recorder=False)
    on = s.flag("live", variants=["a"])
    s.segment(on, "all", 100, [("a", 100)])
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    s.ok("POST", "/flags/1/tags", {"value": "web"})
    s.ok("POST", "/flags/1/tags", {"value": "core"})
    off = s.flag("dark", variants=["a"])
    s.segment(off, "all", 100, [("a", 100)])
    s.flag("noseg", variants=["z"], enabled=True)
    s.ok("POST", "/flags/3/tags", {"value": "core"})
    nodist = s.flag("nodist", variants=["z"], enabled=True)
    nodist_seg = s.segment(nodist, "s", 100)
    s.sync()

    def check(request, expected):
        r = s.evaluate(dict(request, enableDebug=True))
        assert_doc(r, dict(expected, recordSource="evaluation", evalContext=dict(request, enableDebug=True)), what=json.dumps(request))
        assert_timestamped(r)

    text = "<message>"
    check({"entityID": "e1", "flagID": 999}, {"flagID": 999, "evalDebugLog": {"msg": text}})
    check({"entityID": "e1", "flagKey": "nosuch"}, {"flagKey": "nosuch", "evalDebugLog": {"msg": text}})
    check({"entityID": "e1"}, {"evalDebugLog": {"msg": text}})
    check({"entityID": "e1", "flagID": 2}, {"flagID": 2, "flagKey": "dark", "flagSnapshotID": 11, "evalDebugLog": {"msg": text}})
    check({"entityID": "e1", "flagID": 3}, {"flagID": 3, "flagKey": "noseg", "flagSnapshotID": 15, "flagTags": ["core"], "evalDebugLog": {"msg": text}})
    check({"entityID": "e1", "flagID": 4}, {"flagID": 4, "flagKey": "nodist", "flagSnapshotID": 19,
                                            "evalDebugLog": {"segmentDebugLogs": [{"segmentID": nodist_seg, "msg": text}]}})
    live = {"flagID": 1, "flagKey": "live", "flagSnapshotID": 7, "flagTags": ["web", "core"], "segmentID": 1, "variantID": 1,
            "variantKey": "a", "evalDebugLog": {"segmentDebugLogs": [{"segmentID": 1, "msg": text}]}}
    # flagID takes precedence over flagKey; an unknown flagID falls back to the key.
    check({"entityID": "e1", "flagID": 1, "flagKey": "dark"}, live)
    check({"entityID": "e1", "flagID": 999, "flagKey": "live"}, live)
    check({"entityID": "e1", "flagID": 2, "flagKey": "live"}, {"flagID": 2, "flagKey": "dark", "flagSnapshotID": 11, "evalDebugLog": {"msg": text}})
    r = s.evaluate({"flagID": 0})
    assert_doc(r, {"evalContext": {}, "recordSource": "evaluation", "evalDebugLog": {"msg": text}})


def test_missing_entity_id_is_generated(flagr):
    s = flagr(recorder=False)
    f = s.flag("live", variants=["a"])
    s.segment(f, "all", 100, [("a", 100)])
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    s.sync()
    seen = set()
    for _ in range(3):
        r = s.evaluate({"flagID": 1, "entityContext": {"k": 1}})
        entity = r["evalContext"].get("entityID", "")
        assert RANDOM_ENTITY.match(entity), r
        seen.add(entity)
        assert_doc(r, {"evalContext": {"flagID": 1, "entityContext": {"k": 1}}, "flagID": 1, "flagKey": "live", "flagSnapshotID": 5,
                       "segmentID": 1, "variantID": 1, "variantKey": "a", "recordSource": "evaluation"}, volatile={"timestamp", "entityID"})
    assert len(seen) > 1
    # A blank result for an unknown flag keeps the request's (empty) entityID.
    assert s.evaluate({"flagID": 77})["evalContext"].get("entityID", "") == ""


def test_debug_logs_follow_request_and_server_switch(flagr):
    s = flagr(recorder=False)
    f = s.flag("dbg", variants=["a"])
    s.segment(f, "s", 100, [("a", 100)], [("k", "EQ", '"v"')])
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    s.sync()
    r = s.evaluate({"entityID": "e1", "flagID": 1, "entityContext": {"k": "w"}})
    assert segment_logs(r) == [], r
    r = s.evaluate({"entityID": "e1", "flagID": 1, "entityContext": {"k": "w"}, "enableDebug": False})
    assert segment_logs(r) == [], r
    batch = s.batch({"entities": [{"entityID": "e1", "entityContext": {"k": "w"}}], "flagIDs": [1], "enableDebug": True})
    assert [x.get("segmentID") for x in segment_logs(batch[0])] == [1]
    assert batch[0]["evalContext"].get("enableDebug") is True

    quiet = flagr(recorder=False, env={"FLAGR_EVAL_DEBUG_ENABLED": "false"})
    f = quiet.flag("dbg", variants=["a"])
    quiet.segment(f, "s", 100, [("a", 100)], [("k", "EQ", '"v"')])
    quiet.ok("PUT", "/flags/1/enabled", {"enabled": True})
    quiet.sync()
    for context, variant in (({"k": "v"}, "a"), ({"k": "w"}, None)):
        r = quiet.evaluate({"entityID": "e1", "flagID": 1, "entityContext": context, "enableDebug": True})
        want = {"evalContext": {"entityID": "e1", "flagID": 1, "entityContext": context, "enableDebug": True},
                "flagID": 1, "flagKey": "dbg", "flagSnapshotID": 6, "recordSource": "evaluation"}
        if variant:
            want.update(segmentID=1, variantID=1, variantKey="a")
        assert_doc(r, want)


def test_evaluation_request_validation(flagr):
    s = flagr(recorder=False)
    for raw, status in [(b"", 422), (b"{bad", 400), (b'{"entityID": 5, "flagID": 1}', 400), (b'{"flagID": -1}', 422),
                        (b'{"flagID": "one"}', 400), (b'{"flagTagsOperator": "XOR"}', 422)]:
        r = s.call("POST", "/evaluation", raw=raw)
        assert r.status == status, (raw, r)
        assert r.json and r.json.get("message"), r
    for body, status in [({"entities": [], "flagIDs": [1]}, 422), ({"flagIDs": [1]}, 422),
                         ({"entities": [{"entityID": "t"}], "flagIDs": []}, 422),
                         ({"entities": [{"entityID": "t"}], "flagKeys": []}, 422),
                         ({"entities": [{"entityID": "t"}], "flagTags": ["x"], "flagTagsOperator": "XOR"}, 422),
                         ({"entities": [{"entityID": "t"}], "flagIDs": [0]}, 422)]:
        r = s.call("POST", "/evaluation/batch", body)
        assert r.status == status, (body, r)
    assert s.call("POST", "/evaluation/batch", raw=b"[").status == 400
    assert s.batch({"entities": [{"entityID": "t1"}]}) == []


def tagged_flags(s):
    split = s.flag("split", variants=["a", "b", "c"])
    s.segment(split, "all", 100, [("a", 20), ("b", 30), ("c", 50)])
    half = s.flag("half", variants=["on", "off"])
    s.segment(half, "all", 50, [("on", 50), ("off", 50)])
    s.flag("noseg", variants=["z"])
    for fid in (1, 2, 3):
        s.ok("PUT", f"/flags/{fid}/enabled", {"enabled": True})
    s.ok("PUT", "/flags/2", {"entityType": "account"})
    s.ok("POST", "/flags/1/tags", {"value": "web"})
    s.ok("POST", "/flags/1/tags", {"value": "core"})
    s.ok("POST", "/flags/2/tags", {"value": "web"})
    s.ok("POST", "/flags/3/tags", {"value": "core"})
    s.sync()
    snap = dict(s.rows("SELECT id, snapshot_id FROM flags WHERE id <= 3"))
    t1 = {
        1: {"flagID": 1, "flagKey": "split", "flagSnapshotID": snap[1], "flagTags": ["web", "core"], "segmentID": 1, "variantID": 3, "variantKey": "c"},
        2: {"flagID": 2, "flagKey": "half", "flagSnapshotID": snap[2], "flagTags": ["web"], "segmentID": 2, "variantID": 4, "variantKey": "on"},
        3: {"flagID": 3, "flagKey": "noseg", "flagSnapshotID": snap[3], "flagTags": ["core"], "evalDebugLog": {"msg": "<message>"}},
    }
    return t1


def result(base, fid, context):
    out = dict(base[fid], recordSource="evaluation", evalContext=dict(context))
    if fid == 2:
        out["evalContext"]["entityType"] = "account"
    return out


def test_batch_selectors_and_result_order(flagr):
    s = flagr(recorder=False)
    t1 = tagged_flags(s)
    entities = [{"entityID": "t1", "entityContext": {"k": 1}}, {"entityID": "user-0", "entityType": "user"}]
    # user-0 gets variant a of split and falls outside the 50% rollout of half.
    u0 = {1: dict(t1[1], variantID=1, variantKey="a"), 2: {k: v for k, v in t1[2].items() if k not in ("segmentID", "variantID", "variantKey")},
          3: dict(t1[3])}
    expected_by_entity = {"t1": t1, "user-0": u0}
    results = s.batch({"entities": entities, "flagTags": ["web"], "flagKeys": ["half", "half", "split"], "flagIDs": [3, 3, 1]})
    # Per entity: flags selected by tag (in no particular order), then flagIDs, then flagKeys, without repeats.
    assert len(results) == 2 * (2 + 2 + 2), results
    for n, entity in enumerate(entities):
        chunk = results[6 * n:6 * n + 6]
        base = {k: v for k, v in entity.items()}
        table = expected_by_entity[entity["entityID"]]
        assert_unordered(chunk[:2], [result(table, fid, dict(base, flagTags=["web"])) for fid in (1, 2)])
        assert_doc(chunk[2:], [result(table, 3, dict(base, flagID=3)), result(table, 1, dict(base, flagID=1)),
                               result(table, 2, dict(base, flagKey="half")), result(table, 1, dict(base, flagKey="split"))])
    one = {"entityID": "t1"}
    got = s.batch({"entities": [one], "flagTags": ["web", "core"], "flagTagsOperator": "ALL"})
    assert_doc(got, [result(t1, 1, dict(one, flagTags=["web", "core"], flagTagsOperator="ALL"))])
    got = s.batch({"entities": [one], "flagTags": ["web", "core"], "flagTagsOperator": "ANY"})
    assert_unordered(got, [result(t1, fid, dict(one, flagTags=["web", "core"], flagTagsOperator="ANY")) for fid in (1, 2, 3)])
    got = s.batch({"entities": [one], "flagTags": ["core", "nothing"], "flagTagsOperator": "ALL"})
    assert got == []
    got = s.batch({"entities": [one], "flagTags": ["core", "nothing"]})
    assert_unordered(got, [result(t1, fid, dict(one, flagTags=["core", "nothing"])) for fid in (1, 3)])
    assert s.batch({"entities": [one], "flagTags": ["nothing"]}) == []
    got = s.batch({"entities": [one], "flagIDs": [999], "flagKeys": ["nosuch"]})
    assert_doc(got, [{"evalContext": {"entityID": "t1", "flagID": 999}, "flagID": 999, "recordSource": "evaluation", "evalDebugLog": {"msg": "x"}},
                     {"evalContext": {"entityID": "t1", "flagKey": "nosuch"}, "flagKey": "nosuch", "recordSource": "evaluation", "evalDebugLog": {"msg": "x"}}])


def test_get_forms_match_post(flagr):
    s = flagr(recorder=False)
    t1 = tagged_flags(s)
    ctx = {"entityID": "t1", "flagID": 1, "entityContext": {"plan": "pro"}}
    r = s.call("GET", "/evaluation", query={"json": json.dumps(ctx)})
    assert r.status == 200, r
    assert_doc(r.json, result(t1, 1, ctx))
    assert_doc(r.json, s.evaluate(ctx))
    batch = {"entities": [{"entityID": "t1"}], "flagKeys": ["half", "noseg"]}
    r = s.call("GET", "/evaluation/batch", query={"json": json.dumps(batch)})
    assert r.status == 200, r
    assert_doc(r.json["evaluationResults"], [result(t1, 2, {"entityID": "t1", "flagKey": "half"}), result(t1, 3, {"entityID": "t1", "flagKey": "noseg"})])
    for path, query, status in [
        ("/evaluation", None, 422),
        ("/evaluation", {"json": "{bad"}, 400),
        ("/evaluation", {"json": json.dumps({"flagID": -3})}, 400),
        ("/evaluation/batch", None, 422),
        ("/evaluation/batch", {"json": json.dumps({"flagKeys": ["split"]})}, 400),
        ("/evaluation/batch", {"json": json.dumps({"entities": [], "flagKeys": ["split"]})}, 400),
        ("/evaluation/batch", {"json": "[1"}, 400),
    ]:
        r = s.call("GET", path, query=query)
        assert r.status == status, (path, query, r)
        assert r.json and r.json.get("message"), r


def test_batch_size_limit(flagr):
    s = flagr(recorder=False, env={"FLAGR_EVAL_BATCH_SIZE": "3"})
    f = s.flag("lim", variants=["a"])
    s.segment(f, "s", 100, [("a", 100)])
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    s.sync()
    three = [{"entityID": x} for x in "abc"]
    assert len(s.batch({"entities": three, "flagIDs": [1]})) == 3
    # Duplicated selectors are removed before counting; a tag list counts as one flag.
    assert len(s.batch({"entities": three[:2], "flagKeys": ["lim", "lim", "lim"]})) == 2
    for body in ({"entities": three[:2], "flagIDs": [1], "flagKeys": ["lim"]},
                 {"entities": three[:2], "flagIDs": [1], "flagTags": ["x"]},
                 {"entities": three + [{"entityID": "d"}], "flagIDs": [1]}):
        r = s.call("POST", "/evaluation/batch", body)
        assert r.status == 400 and r.json.get("message"), (body, r)
    r = s.call("GET", "/evaluation/batch", query={"json": json.dumps({"entities": three + [{"entityID": "d"}], "flagIDs": [1]})})
    assert r.status == 400, r
    assert len(s.batch({"entities": three[:1], "flagTags": ["x", "y", "z"]})) == 0
