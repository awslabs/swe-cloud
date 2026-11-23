"""Multi-step scenarios: the refreshed evaluation view, flag life cycle as seen by
evaluation, duplicated flags and recording toggles."""

import time

from flagr_support import assert_doc, frame_count, string_payload


def wait_for(server, request, predicate, timeout):
    deadline = time.time() + timeout
    while True:
        result = server.evaluate(request)
        if predicate(result) or time.time() > deadline:
            return result
        time.sleep(0.1)


def test_writes_reach_evaluation_only_after_a_refresh(flagr):
    s = flagr(recorder=False, refresh="10s")
    f = s.flag("slow", variants=["a", "b"])
    seg = s.segment(f, "all", 100, [("a", 100)])
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    request = {"entityID": "e1", "flagID": 1}
    # The view loaded at start-up does not know the new flag yet; the management API does.
    stale = s.evaluate(request)
    assert_doc(stale, {"evalContext": request, "flagID": 1, "recordSource": "evaluation", "evalDebugLog": {"msg": "x"}})
    assert s.ok("GET", "/flags/1")["enabled"] is True
    fresh = wait_for(s, request, lambda r: r.get("variantKey"), 14)
    assert_doc(fresh, {"evalContext": request, "flagID": 1, "flagKey": "slow", "flagSnapshotID": 6, "segmentID": seg,
                       "variantID": 1, "variantKey": "a", "recordSource": "evaluation"})
    # Right after a refresh, a change stays invisible until the next one.
    s.ok("PUT", f"/flags/1/segments/{seg}/distributions", {"distributions": [{"variantID": 2, "variantKey": "b", "percent": 100}]})
    assert [d["variantKey"] for d in s.ok("GET", f"/flags/1/segments/{seg}/distributions")] == ["b"]
    assert s.evaluate(request).get("variantKey") == "a"
    changed = wait_for(s, request, lambda r: r.get("variantKey") == "b", 14)
    assert_doc(changed, {"evalContext": request, "flagID": 1, "flagKey": "slow", "flagSnapshotID": 7, "segmentID": seg,
                         "variantID": 2, "variantKey": "b", "recordSource": "evaluation"})


def test_flag_life_cycle_as_seen_by_evaluation(flagr):
    s = flagr(recorder=False)
    f = s.flag("life", variants=[("blue", {"hex": "#00f"}), "green"])
    seg = s.segment(f, "ca", 100, [("blue", 100)], [("state", "EQ", '"CA"')])
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    s.sync()
    ca = {"entityID": "u1", "flagKey": "life", "entityContext": {"state": "CA"}}
    hit = {"evalContext": ca, "flagID": 1, "flagKey": "life", "flagSnapshotID": 7, "segmentID": seg, "variantID": 1,
           "variantKey": "blue", "variantAttachment": {"hex": "#00f"}, "recordSource": "evaluation"}
    assert_doc(s.evaluate(ca), hit)
    # Editing the variant changes the evaluated key and attachment.
    s.ok("PUT", "/flags/1/variants/1", {"key": "navy", "attachment": {"hex": "#008"}})
    s.sync()
    hit.update(variantKey="navy", variantAttachment={"hex": "#008"}, flagSnapshotID=9)
    assert_doc(s.evaluate(ca), hit)
    # Widening the constraint lets other states in.
    s.ok("PUT", "/flags/1/segments/1/constraints/1", {"property": "state", "operator": "IN", "value": '["CA","NY"]'})
    s.sync()
    ny = dict(ca, entityContext={"state": "NY"})
    hit.update(flagSnapshotID=11)
    assert_doc(s.evaluate(ny), dict(hit, evalContext=ny))
    s.ok("PUT", "/flags/1/enabled", {"enabled": False})
    s.sync()
    assert_doc(s.evaluate(ca), {"evalContext": ca, "flagID": 1, "flagKey": "life", "flagSnapshotID": 13,
                                "recordSource": "evaluation", "evalDebugLog": {"msg": "x"}})
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    s.ok("DELETE", "/flags/1")
    s.sync()
    assert_doc(s.evaluate(ca), {"evalContext": ca, "flagKey": "life", "recordSource": "evaluation", "evalDebugLog": {"msg": "x"}})
    by_id = {"entityID": "u1", "flagID": 1, "entityContext": {"state": "CA"}}
    assert_doc(s.evaluate(by_id), {"evalContext": by_id, "flagID": 1, "recordSource": "evaluation", "evalDebugLog": {"msg": "x"}})
    s.ok("PUT", "/flags/1/restore")
    s.sync()
    assert_doc(s.evaluate(ca), dict(hit, flagSnapshotID=18))
    # Renaming the key moves evaluation by key.
    s.ok("PUT", "/flags/1", {"key": "life-v2"})
    s.sync()
    assert s.evaluate(ca).get("flagID", 0) == 0
    renamed = dict(ca, flagKey="life-v2")
    assert_doc(s.evaluate(renamed), dict(hit, evalContext=renamed, flagKey="life-v2", flagSnapshotID=20))


SOURCE = "a c c c c c c c b b".split()
COPY = "c a a c c c b c b b".split()


def test_duplicate_flag_buckets_by_its_own_id(flagr):
    s = flagr(recorder=False)
    split = s.flag("split", variants=["a", "b", "c"])
    s.segment(split, "all", 100, [("a", 20), ("b", 30), ("c", 50)])
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    for i in range(8):
        s.ok("POST", "/flags", {"description": "filler", "key": f"filler-{i}"})
    copy = s.ok("POST", "/flags/1/duplicate", {"key": "split-copy"})
    assert copy["id"] == 10 and [v["id"] for v in copy["variants"]] == [4, 5, 6], copy
    s.sync()
    entities = [{"entityID": f"user-{i}"} for i in range(10)]
    results = s.batch({"entities": entities, "flagKeys": ["split", "split-copy"]})
    assert [r.get("variantKey") for r in results[0::2]] == SOURCE
    assert [r.get("variantKey") for r in results[1::2]] == COPY
    assert [r.get("variantID") for r in results[1::2]] == [{"a": 4, "b": 5, "c": 6}[k] for k in COPY]
    assert all(r.get("flagID") == 10 and r.get("segmentID") == 2 for r in results[1::2]), results
    # The copy is independent of later changes to its source.
    s.ok("PUT", "/flags/1/enabled", {"enabled": False})
    s.ok("PUT", "/flags/1/segments/1/distributions", {"distributions": [{"variantID": 1, "variantKey": "a", "percent": 100}]})
    s.sync()
    results = s.batch({"entities": entities, "flagKeys": ["split", "split-copy"]})
    assert [r.get("variantKey") for r in results[1::2]] == COPY
    assert all(not r.get("variantKey") for r in results[0::2])


def test_recording_follows_flag_edits(flagr):
    s = flagr()
    f = s.flag("rec", variants=[("a", {"v": 1})], records=True, enabled=True)
    s.segment(f, "all", 100, [("a", 100)])
    s.sync()
    first = s.evaluate({"entityID": "q0", "flagID": 1})
    s.ok("PUT", "/flags/1/variants/1", {"key": "renamed", "attachment": {"v": 2}})
    s.ok("PUT", "/flags/1", {"entityType": "team"})
    s.sync()
    second = s.evaluate({"entityID": "q1", "flagID": 1, "entityType": "user"})
    assert second.get("variantKey") == "renamed" and second["evalContext"]["entityType"] == "team", second
    s.ok("PUT", "/flags/1", {"dataRecordsEnabled": False})
    s.sync()
    silent = s.evaluate({"entityID": "q2", "flagID": 1})
    assert silent.get("variantKey") == "renamed"
    s.ok("PUT", "/flags/1", {"dataRecordsEnabled": True})
    s.sync()
    third = s.evaluate({"entityID": "q3", "flagID": 1})
    records = s.records(lambda rs: frame_count(rs) >= 3, settle=2.0)
    payloads = [string_payload(fr) for r in records for fr in r.frames]
    assert sorted(p["evalContext"]["entityID"] for p in payloads) == ["q0", "q1", "q3"], payloads
    for response in (first, second, third):
        recorded = next(p for p in payloads if p["evalContext"]["entityID"] == response["evalContext"]["entityID"])
        assert_doc(recorded, response)
    assert [next(p for p in payloads if p["evalContext"]["entityID"] == e).get("flagSnapshotID") for e in ("q0", "q1", "q3")] == [6, 9, 13]
