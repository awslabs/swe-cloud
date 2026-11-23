"""Kinesis data records: which evaluations are recorded, the frame in both output
modes, and how frames are packed into plain or KPL-aggregated Kinesis records."""

import json

from flagr_support import RANDOM_ENTITY, assert_doc, frame_count, raw_payload, same, string_payload

ATTACHMENT = {"color": "red", "w": 1.5}


def recorded_flags(s, entity_type=None):
    rec = s.flag("rec", variants=[("a", ATTACHMENT), "b"])
    s.segment(rec, "ca", 100, [("a", 50), ("b", 50)], [("state", "EQ", '"CA"')])
    extra = {"entityType": entity_type} if entity_type else {}
    s.ok("PUT", "/flags/1", {"dataRecordsEnabled": True, **extra})
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    norec = s.flag("norec", variants=["a"])
    s.segment(norec, "all", 100, [("a", 100)])
    s.ok("PUT", "/flags/2/enabled", {"enabled": True})
    s.sync()


def assert_aggregated_record(record):
    """KPL envelope as the reference writes it: one partition key (the first frame's
    entityID, also used as the record's key) shared by every frame."""
    assert record.aggregated, record.data[:80]
    assert record.hash_key_table == [], record.hash_key_table
    first = json.loads(record.frames[0]["payload"]) if isinstance(record.frames[0]["payload"], str) else record.frames[0]["payload"]
    assert record.key_table == [first["evalContext"]["entityID"]], (record.key_table, first)
    assert record.partition_key == record.key_table[0]
    assert [index for index, _ in record.entries] == [0] * len(record.entries)


def assert_payloads(observed, expected):
    """Recorded results equal the API results (multiset, timestamps excluded)."""
    rest = list(observed)
    for want in expected:
        match = next((i for i, got in enumerate(rest) if _equal(got, want)), None)
        assert match is not None, f"no record for {json.dumps(want, sort_keys=True)}\n records: {json.dumps(rest, sort_keys=True)}"
        rest.pop(match)
    assert not rest, f"unexpected records: {json.dumps(rest, sort_keys=True)}"


def _equal(got, want):
    try:
        assert_doc(got, want)
        return True
    except AssertionError:
        return False


def test_default_records_are_aggregated_string_frames(flagr):
    s = flagr()
    recorded_flags(s)
    responses = [
        s.evaluate({"entityID": "u1", "entityType": "user", "flagID": 1, "entityContext": {"state": "CA"}, "enableDebug": True}),
        s.evaluate({"entityID": "u2", "flagKey": "rec", "entityContext": {"state": "NY"}}),
        s.evaluate({"entityID": "u3", "flagID": 2, "entityContext": {"state": "CA"}}),
        s.evaluate({"entityID": "u4", "flagID": 999}),
        s.evaluate({"flagID": 1, "entityContext": {"state": "CA"}}),
    ]
    responses += s.batch({"entities": [{"entityID": "b1", "entityContext": {"state": "CA"}}, {"entityID": "b2", "entityContext": {"state": "CA"}}],
                          "flagIDs": [1, 2]})
    responses.append(s.ok("GET", "/evaluation", query={"json": json.dumps({"entityID": "g1", "flagID": 1, "entityContext": {"state": "CA"}})}))
    # Records are written only for the flag with dataRecordsEnabled, matching or not.
    expected = [r for r in responses if r.get("flagID") == 1]
    assert [r["evalContext"].get("entityID") for r in expected][:2] == ["u1", "u2"] and len(expected) == 6
    assert RANDOM_ENTITY.match(expected[2]["evalContext"]["entityID"])
    records = s.records(lambda rs: frame_count(rs) >= len(expected), settle=2.0)
    assert frame_count(records) == len(expected), [r.frames for r in records]
    for record in records:
        assert_aggregated_record(record)
    assert_payloads([string_payload(f) for r in records for f in r.frames], expected)
    # The recorded result is the evaluation result, including dataRecordsEnabled.
    u1 = string_payload(next(f for r in records for f in r.frames if string_payload(f)["evalContext"]["entityID"] == "u1"))
    assert u1["dataRecordsEnabled"] is True and u1["variantKey"] in ("a", "b") and [x.get("segmentID") for x in (u1.get("evalDebugLog") or {}).get("segmentDebugLogs") or []] == [1]


def test_raw_json_frames_as_plain_records(flagr):
    s = flagr(env={"FLAGR_RECORDER_FRAME_OUTPUT_MODE": "payload_raw_json", "FLAGR_RECORDER_KINESIS_AGGREGATE_BATCH_SIZE": "1"})
    recorded_flags(s, entity_type="device")
    responses = [
        s.evaluate({"entityID": "r1", "entityType": "user", "flagID": 1, "entityContext": {"state": "CA"}}),
        s.evaluate({"entityID": "r2", "flagID": 1, "entityContext": {"state": "TX"}, "enableDebug": True}),
        s.evaluate({"entityID": "r3", "flagID": 1, "entityContext": {"state": "CA", "n": 1e20, "x": 0.1, "t": True, "z": None}}),
        s.evaluate({"flagID": 1}),
    ]
    assert all(r["evalContext"]["entityType"] == "device" for r in responses), responses
    records = s.records(lambda rs: frame_count(rs) >= 4, settle=2.0)
    assert len(records) == 4, [r.data[:60] for r in records]
    payloads = []
    for record in records:
        assert not record.aggregated
        (frame,) = record.frames
        payload = raw_payload(frame)
        # A plain record is keyed by the evaluated entity.
        assert record.partition_key == payload["evalContext"]["entityID"], (record.partition_key, payload)
        payloads.append(payload)
    assert_payloads(payloads, responses)
    r3 = next(p for p in payloads if p["evalContext"]["entityID"] == "r3")
    assert same(r3["evalContext"]["entityContext"], {"state": "CA", "n": 1e20, "x": 0.1, "t": True, "z": None})


def packing(records):
    """Entity IDs of the frames in each Kinesis record, as a sorted list of groups."""
    return sorted(tuple(string_payload(f)["evalContext"]["entityID"] for f in r.frames) for r in records)


SLOW_FLUSH = {"FLAGR_RECORDER_KINESIS_FLUSH_INTERVAL": "3s"}


def test_one_batch_shares_aggregated_records(flagr):
    # A long flush interval keeps all frames of one batch request in the same flush.
    s = flagr(env=SLOW_FLUSH)
    recorded_flags(s)
    entities = [{"entityID": f"k{i}", "entityContext": {"state": "CA"}} for i in range(5)]
    responses = s.batch({"entities": entities, "flagIDs": [1]})
    records = s.records(lambda rs: frame_count(rs) >= 5, settle=2.0)
    assert packing(records) == [("k0", "k1", "k2", "k3", "k4")], packing(records)
    (record,) = records
    assert_aggregated_record(record)
    assert record.partition_key == "k0" and record.key_table == ["k0"]
    assert_payloads([string_payload(f) for f in record.frames], responses)


def test_aggregate_batch_count_bounds_frames_per_record(flagr):
    s = flagr(env=dict(SLOW_FLUSH, FLAGR_RECORDER_KINESIS_AGGREGATE_BATCH_COUNT="2"))
    recorded_flags(s)
    entities = [{"entityID": f"k{i}", "entityContext": {"state": "CA"}} for i in range(5)]
    responses = s.batch({"entities": entities, "flagIDs": [1]})
    records = s.records(lambda rs: frame_count(rs) >= 5, settle=2.0)
    assert packing(records) == [("k0", "k1"), ("k2", "k3"), ("k4",)], packing(records)
    for record in records:
        assert_aggregated_record(record)
    assert sorted(r.partition_key for r in records) == ["k0", "k2", "k4"]
    assert_payloads([string_payload(f) for r in records for f in r.frames], responses)

    one = flagr(env={"FLAGR_RECORDER_KINESIS_AGGREGATE_BATCH_COUNT": "1"})
    recorded_flags(one)
    responses = one.batch({"entities": entities[:3], "flagIDs": [1]})
    records = one.records(lambda rs: frame_count(rs) >= 3, settle=2.0)
    # Even a single frame is wrapped in an aggregated envelope.
    assert packing(records) == [("k0",), ("k1",), ("k2",)], packing(records)
    for record in records:
        assert_aggregated_record(record)
        assert record.partition_key == string_payload(record.frames[0])["evalContext"]["entityID"]
    assert_payloads([string_payload(f) for r in records for f in r.frames], responses)


def test_aggregate_batch_size_sends_large_frames_plain(flagr):
    s = flagr(env={"FLAGR_RECORDER_KINESIS_AGGREGATE_BATCH_SIZE": "600"})
    recorded_flags(s)
    big = {"state": "CA", "pad": "x" * 700}
    small = s.batch({"entities": [{"entityID": "s1"}, {"entityID": "s2"}], "flagIDs": [1]})
    large = s.batch({"entities": [{"entityID": "big1", "entityContext": big}, {"entityID": "big2", "entityContext": big}], "flagIDs": [1]})
    records = s.records(lambda rs: frame_count(rs) >= 4, settle=2.0)
    assert frame_count(records) == 4
    plain = [r for r in records if not r.aggregated]
    packed = [r for r in records if r.aggregated]
    assert sorted(r.partition_key for r in plain) == ["big1", "big2"]
    assert_payloads([string_payload(r.frames[0]) for r in plain], large)
    for record in packed:
        assert_aggregated_record(record)
    assert_payloads([string_payload(f) for r in packed for f in r.frames], small)

    # 0 selects the default threshold: everything is aggregated again.
    zero = flagr(env={"FLAGR_RECORDER_KINESIS_AGGREGATE_BATCH_SIZE": "0"})
    recorded_flags(zero)
    responses = zero.batch({"entities": [{"entityID": "big1", "entityContext": big}, {"entityID": "s1"}], "flagIDs": [1]})
    records = zero.records(lambda rs: frame_count(rs) >= 2, settle=2.0)
    assert frame_count(records) == 2 and all(r.aggregated for r in records)
    for record in records:
        assert_aggregated_record(record)
    assert_payloads([string_payload(f) for r in records for f in r.frames], responses)


def test_recording_gates(flagr):
    s = flagr()
    held = s.flag("held", variants=["a"], records=True, enabled=True)
    held_seg = s.segment(held, "nobody", 0, [("a", 100)])
    dark = s.flag("dark", variants=["a"], records=True)
    s.segment(dark, "all", 100, [("a", 100)])
    s.flag("noseg", variants=["a"], records=True, enabled=True)
    quiet = s.flag("quiet", variants=["a"], records=False, enabled=True)
    s.segment(quiet, "all", 100, [("a", 100)])
    s.sync()
    results = s.batch({"entities": [{"entityID": "e1"}, {"entityID": "e2"}], "flagIDs": [1, 2, 3, 4, 777]})
    assert len(results) == 10
    # A matched segment with no variant (0% rollout) is still an evaluation that is recorded.
    held_results = [r for r in results if r.get("flagID") == 1]
    assert all("variantKey" not in r or not r["variantKey"] for r in held_results), held_results
    records = s.records(lambda rs: frame_count(rs) >= 2, settle=3.0)
    payloads = [string_payload(f) for r in records for f in r.frames]
    assert_payloads(payloads, held_results)
    assert all(p.get("segmentID", 0) == 0 and p["flagID"] == 1 for p in payloads), (payloads, held_seg)

    off = flagr(env={"FLAGR_RECORDER_ENABLED": "false"})
    f = off.flag("rec", variants=["a"], records=True, enabled=True)
    off.segment(f, "all", 100, [("a", 100)])
    off.sync()
    for i in range(3):
        assert off.evaluate({"entityID": f"x{i}", "flagID": 1}).get("variantKey") == "a"
    assert off.records(lambda rs: False, timeout=4) == []
