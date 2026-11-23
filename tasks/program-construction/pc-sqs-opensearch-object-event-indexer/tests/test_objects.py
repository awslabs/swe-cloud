"""Single objects: framing, codecs, event fields, timestamps and decompression selection."""

import random
import time
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import assert_same_multiset, gz, parse_instant, same, strip_time, zstd_frames


def day(moment, fmt="%Y.%m.%d"):
    return moment.astimezone(UTC).strftime(fmt)


def base_fields(pipe, key):
    return {"bucket": pipe.bucket, "object": key, "region": "us-east-1", "source_type": "aws_s3"}


def messages_by_object(found):
    grouped = {}
    for hit in found:
        grouped.setdefault(hit["_source"]["object"], []).append(hit["_source"].get("message"))
    return {key: sorted(values) for key, values in grouped.items()}


def test_bytes_codec_emits_one_event_per_line(pipe):
    body = b"first line\nsecond line\n\nfourth\r\n  spaced  \nlast-no-newline"
    head = pipe.put("logs/plain.log", body, ContentType="text/plain")
    time.sleep(1.5)
    pipe.notify("logs/plain.log")
    pipe.start(pipe.config())
    found = pipe.wait_settled(6)
    modified = head["LastModified"]
    for hit in found:
        assert hit["_index"] == f"{pipe.prefix}-{day(modified)}"
        # Bytes-decoded events carry the object's Last-Modified time.
        assert parse_instant(hit["_source"]["timestamp"]) == modified
    expected = [{**base_fields(pipe, "logs/plain.log"), "message": m} for m in ["first line", "second line", "", "fourth\r", "  spaced  ", "last-no-newline"]]
    assert_same_multiset([strip_time(h["_source"]) for h in found], expected)
    assert len({h["_id"] for h in found}) == 6
    assert pipe.stop() == 0


def test_user_metadata_becomes_event_fields(pipe):
    head = pipe.put("meta/plain.log", b"body line\n", Metadata={"owner": "team-a", "Mixed-Case": "MC", "x-y": "dash"})
    pipe.notify("meta/plain.log")
    pipe.start(pipe.config())
    (hit,) = pipe.wait_settled(1)
    source = dict(hit["_source"])
    assert parse_instant(source.pop("timestamp")) == head["LastModified"]
    assert same(source, {**base_fields(pipe, "meta/plain.log"), "message": "body line", "owner": "team-a", "mixed-case": "MC", "x-y": "dash"})
    assert pipe.stop() == 0


def test_user_metadata_overrides_event_fields(pipe):
    metadata = {"bucket": "meta-bucket", "object": "meta-obj", "message": "meta-msg", "source_type": "meta-st", "timestamp": "meta-ts"}
    head = pipe.put("meta/clash.log", b"clash line\n", Metadata=metadata)
    pipe.notify("meta/clash.log")
    pipe.start(pipe.config())
    (hit,) = pipe.wait_settled(1)
    assert same(hit["_source"], {**metadata, "region": "us-east-1"})
    assert hit["_index"] in {f"{pipe.prefix}-{day(head['LastModified'])}", f"{pipe.prefix}-{day(datetime.now(UTC))}"}
    assert pipe.stop() == 0


JSON_LINES = [
    '{"id":"j1","n":5,"f":1.5,"e":1e3,"neg":-2,"b":true,"z":null,"nested":{"k":"v"},"arr":["x","y"],"s":"str"}',
    '{"id":"j2","bucket":"fake","object":"fake","region":"fake","source_type":"fake","s":"j2"}',
    "not json",
    '[{"id":"a1","s":"arr"},{"id":"a2","s":"arr"}]',
    '"just a string"',
    "42",
    '{"id":"j6","s":"t"} trailing',
    "[]",
    '{"id":"j7","s":"after-bad"}',
    '{"id":"j8","message":"own message"}',
]


def test_json_codec_decodes_objects_per_line(pipe):
    head = pipe.put("j/types.json", ("\n".join(JSON_LINES) + "\n").encode())
    time.sleep(2.2)
    pipe.notify("j/types.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"'))
    found = pipe.wait_settled(6)
    now = datetime.now(UTC)
    base = base_fields(pipe, "j/types.json")
    docs = {h["_id"]: h for h in found}
    assert sorted(docs) == ["a1", "a2", "j1", "j2", "j7", "j8"]
    for hit in found:
        stamp = parse_instant(hit["_source"]["timestamp"])
        # JSON-decoded events are stamped when decoded, not with Last-Modified.
        assert head["LastModified"] + timedelta(seconds=1.5) < stamp <= now + timedelta(seconds=1)
        assert hit["_index"] == f"{pipe.prefix}-{day(stamp)}"
    expected = {
        "j1": {**base, "n": 5, "f": 1.5, "e": 1000, "neg": -2, "b": True, "z": None, "nested": {"k": "v"}, "arr": ["x", "y"], "s": "str"},
        "j2": {"bucket": pipe.bucket, "object": "j/types.json", "region": "us-east-1", "source_type": "fake", "s": "j2"},
        "a1": {**base, "s": "arr"},
        "a2": {**base, "s": "arr"},
        "j7": {**base, "s": "after-bad"},
        "j8": {**base, "message": "own message"},
    }
    for key, value in expected.items():
        assert same(strip_time(docs[key]["_source"]), value), (key, docs[key]["_source"])
    assert pipe.stop() == 0


def test_json_own_timestamp_is_kept_and_index_uses_current_date(pipe):
    lines = [
        '{"id":"s1","kind":"str","timestamp":"2020-01-02T03:04:05Z"}',
        '{"id":"s2","kind":"num","timestamp":1700000000}',
        '{"id":"s3","kind":"bad","timestamp":"not a date"}',
        '{"id":"s4","kind":"none"}',
        '{"id":"s5","kind":"nul","timestamp":null}',
    ]
    pipe.put("ts.json", ("\n".join(lines) + "\n").encode())
    started = datetime.now(UTC)
    pipe.notify("ts.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"', index=pipe.prefix + "-{{ kind }}-%Y.%m.%d"))
    found = pipe.wait_settled(5)
    dates = {day(started), day(datetime.now(UTC))}
    docs = {h["_id"]: h for h in found}
    assert sorted(docs) == ["s1", "s2", "s3", "s4", "s5"]
    for key, hit in docs.items():
        kind = hit["_source"]["kind"]
        assert hit["_index"].rsplit("-", 1)[0] == f"{pipe.prefix}-{kind}", key
        assert hit["_index"].rsplit("-", 1)[1] in dates, hit["_index"]
    assert same(docs["s1"]["_source"]["timestamp"], "2020-01-02T03:04:05Z")
    assert same(docs["s2"]["_source"]["timestamp"], 1700000000)
    assert same(docs["s3"]["_source"]["timestamp"], "not a date")
    assert same(docs["s5"]["_source"]["timestamp"], None)
    assert started - timedelta(seconds=1) <= parse_instant(docs["s4"]["_source"]["timestamp"])
    assert pipe.stop() == 0


DETECTED = {
    "c/enc.log": ({"ContentEncoding": "gzip"}, gz(b"enc1\nenc2\n"), ["enc1", "enc2"]),
    "c/enczstd.log": ({"ContentEncoding": "zstd"}, zstd_frames(b"ez1\n"), ["ez1"]),
    "c/type.log": ({"ContentType": "application/x-gzip"}, gz(b"type1\n"), ["type1"]),
    "c/type2.log": ({"ContentType": "application/gzip"}, gz(b"type2\n"), ["type2"]),
    "c/ctzstd.dat": ({"ContentType": "application/zstd"}, zstd_frames(b"ctz1\n"), ["ctz1"]),
    "c/ext.gz": ({}, gz(b"ext1\n"), ["ext1"]),
    "c/frames.zst": ({}, zstd_frames(b"zf1\nzf2\n", b"zf3\n"), ["zf1", "zf2", "zf3"]),
    "c/multi.log.gz": ({}, gz(b"mm1\n") + gz(b"mm2\nmm3\n"), ["mm1", "mm2", "mm3"]),
}


def test_compression_detected_from_encoding_type_or_extension(pipe):
    for key, (options, body, _lines) in DETECTED.items():
        pipe.put(key, body, **options)
        pipe.notify(key)
    pipe.start(pipe.config())
    found = pipe.wait_settled(sum(len(lines) for _o, _b, lines in DETECTED.values()))
    assert messages_by_object(found) == {key: sorted(lines) for key, (_o, _b, lines) in DETECTED.items()}
    assert pipe.stop() == 0


PRECEDENCE = {
    # Content-Type beats the key extension.
    "p/zstd-type.gz": ({"ContentType": "application/zstd"}, zstd_frames(b"zstd-by-type\n"), "zstd-by-type"),
    # Content-Encoding beats Content-Type.
    "p/gzip-encoding.zst": ({"ContentEncoding": "gzip", "ContentType": "application/zstd"}, gz(b"gzip-by-encoding\n"), "gzip-by-encoding"),
    "p/gzip-encoding2.log": ({"ContentEncoding": "gzip", "ContentType": "application/zstd"}, gz(b"gzip-by-encoding2\n"), "gzip-by-encoding2"),
    # Unrecognised header values fall through to the next hint.
    "p/identity.gz": ({"ContentEncoding": "identity"}, gz(b"identity-then-extension\n"), "identity-then-extension"),
    "p/text-type.gz": ({"ContentType": "text/plain"}, gz(b"text-type-then-extension\n"), "text-type-then-extension"),
    # Only the final extension counts.
    "p/dir.gz/file.log": ({}, b"plain-in-gz-directory\n", "plain-in-gz-directory"),
    "p/double.log.gz.txt": ({}, b"plain-double-extension\n", "plain-double-extension"),
}


def test_compression_hint_precedence(pipe):
    for key, (options, body, _line) in PRECEDENCE.items():
        pipe.put(key, body, **options)
        pipe.notify(key)
    pipe.start(pipe.config())
    found = pipe.wait_settled(len(PRECEDENCE))
    assert messages_by_object(found) == {key: [line] for key, (_o, _b, line) in PRECEDENCE.items()}
    assert pipe.stop() == 0


RAW = {
    "r/nohint.bin": ({}, "hidden-a"),
    "r/upper.GZ": ({}, "hidden-b"),
    "r/archive.tgz": ({}, "hidden-c"),
    "r/params.log": ({"ContentType": "application/gzip; charset=binary"}, "hidden-d"),
    "r/xgzip.log": ({"ContentEncoding": "x-gzip"}, "hidden-e"),
    "r/.gz": ({}, "hidden-f"),
}


def test_unrecognised_hints_leave_bytes_undecompressed(pipe):
    for key, (options, text) in RAW.items():
        pipe.put(key, gz(text.encode() + b"\n"), **options)
        pipe.notify(key)
    pipe.put("r/control.log", b"control\n")
    pipe.notify("r/control.log")
    pipe.start(pipe.config())
    pipe.wait_for(lambda: pipe.queued() == 0 and {h["_source"]["object"] for h in pipe.hits()} >= {*RAW, "r/control.log"}, message="all objects indexed")
    time.sleep(2)
    grouped = messages_by_object(pipe.hits())
    assert grouped["r/control.log"] == ["control"]
    # Invalid UTF-8 in an undecoded stream is replaced with U+FFFD (observed natively).
    assert grouped["r/nohint.bin"] == [
        "\x1f\ufffd\x08\x00\x00\x00\x00\x00\x02\x03\ufffd\ufffdLII\ufffd\ufffdM\ufffd\x02\x00~\ufffd\x07\x04\t\x00\x00\x00"
    ]
    for key, (_options, text) in RAW.items():
        lines = grouped[key]
        assert text not in lines, key
        # The undecoded gzip stream starts with its magic byte 0x1f.
        assert any(line.startswith("\x1f") for line in lines), (key, lines)
    assert pipe.queued() == 0
    assert pipe.stop() == 0


def test_explicit_compression_overrides_detection(pipe):
    pipe.put("f/raw.gz", b"rawtext\n")
    pipe.put("f/raw2.log", b"rawtext2\n", ContentEncoding="gzip")
    pipe.notify("f/raw.gz")
    pipe.notify("f/raw2.log")
    config = pipe.config().read_text().replace('compression = "auto"', 'compression = "none"')
    pipe.start(pipe.config(text=config))
    found = pipe.wait_settled(2)
    assert messages_by_object(found) == {"f/raw.gz": ["rawtext"], "f/raw2.log": ["rawtext2"]}
    assert pipe.stop() == 0


@pytest.mark.parametrize(("method", "key", "body"), [("gzip", "f/hidden.zst", gz(b"forced one\nforced two\n")), ("zstd", "f/hidden.gz", zstd_frames(b"forced one\n", b"forced two\n"))], ids=["gzip", "zstd"])
def test_forced_compression_ignores_extension(pipe, method, key, body):
    pipe.put(key, body)
    pipe.notify(key)
    config = pipe.config().read_text().replace('compression = "auto"', f'compression = "{method}"')
    pipe.start(pipe.config(text=config))
    found = pipe.wait_settled(2)
    assert messages_by_object(found) == {key: ["forced one", "forced two"]}
    assert pipe.stop() == 0


def test_empty_objects_produce_no_events(pipe):
    pipe.put("e/empty.log", b"")
    pipe.put("e/empty.gz", b"")
    pipe.put("e/control.log", b"control\n")
    for key in ("e/empty.log", "e/empty.gz", "e/control.log"):
        pipe.notify(key)
    pipe.start(pipe.config())
    found = pipe.wait_settled(1)
    assert messages_by_object(found) == {"e/control.log": ["control"]}
    assert pipe.stop() == 0


def test_corrupt_compressed_object_keeps_message(pipe):
    rng = random.Random(7)
    lines = [f"line-{i:04d}-{rng.getrandbits(64):016x}" for i in range(3000)]
    packed = gz("".join(line + "\n" for line in lines).encode())
    pipe.put("bad/truncated.gz", packed[: len(packed) // 2])
    pipe.put("bad/not-gzip.gz", b"not gzip at all\nsecond\n")
    pipe.put("bad/control.log", b"control\n")
    pipe.notify("bad/truncated.gz", tag="truncated")
    pipe.notify("bad/not-gzip.gz", tag="not-gzip")
    pipe.notify("bad/control.log", tag="control")
    pipe.start(pipe.config(visibility=3))
    pipe.wait_for(lambda: pipe.queued() == 2 and any(h["_source"]["object"] == "bad/control.log" for h in pipe.hits()), message="control processed")
    time.sleep(8)
    grouped = messages_by_object(pipe.hits())
    assert set(grouped["bad/control.log"]) == {"control"}
    assert "bad/not-gzip.gz" not in grouped
    # Lines decoded before the stream broke are still delivered (and re-delivered on retry).
    partial = grouped["bad/truncated.gz"]
    assert lines[0] in partial
    assert set(partial) <= set(lines)
    assert set(partial) != set(lines)
    assert pipe.stop() == 0
    left = pipe.remaining()
    assert sorted(tag for tag, _count in left.values()) == ["not-gzip", "truncated"]
    assert all(count >= 3 for _tag, count in left.values()), left


def test_multiline_continue_through(pipe):
    pipe.put("m/trace.log", b"ERROR one\n  at a\n  at b\nINFO two\nERROR three\n  at c")
    pipe.put("m/flat.log", b"abc\ndef\n")
    pipe.notify("m/trace.log")
    pipe.notify("m/flat.log")
    source = 'multiline.start_pattern = "^[A-Za-z]"\nmultiline.condition_pattern = "^\\\\s"\nmultiline.mode = "continue_through"\nmultiline.timeout_ms = 1000'
    pipe.start(pipe.config(source=source))
    found = pipe.wait_settled(5)
    assert messages_by_object(found) == {"m/trace.log": sorted(["ERROR one\n  at a\n  at b", "INFO two", "ERROR three\n  at c"]), "m/flat.log": ["abc", "def"]}
    assert pipe.stop() == 0


def test_multiline_halt_with(pipe):
    pipe.put("m/halt.log", b"abc\ndef\ngeh\nxyz\nabc\nend")
    pipe.notify("m/halt.log")
    source = 'multiline.start_pattern = "abc"\nmultiline.condition_pattern = "geh"\nmultiline.mode = "halt_with"\nmultiline.timeout_ms = 1000'
    pipe.start(pipe.config(source=source))
    found = pipe.wait_settled(3)
    assert messages_by_object(found) == {"m/halt.log": sorted(["abc\ndef\ngeh", "xyz", "abc\nend"])}
    assert pipe.stop() == 0


MULTILINE_MODES = {
    "continue_past": (
        '"\\\\\\\\$"',
        '"\\\\\\\\$"',
        b"a1 \\\na2 \\\na3\nb1\nc1 \\\nc2",
        ["a1 \\\na2 \\\na3", "b1", "c1 \\\nc2"],
    ),
    "halt_before": (
        '"^START"',
        '"^START"',
        b"START one\nx1\nx2\nSTART two\nSTART three\ny1\nfree\nSTART four",
        ["START one\nx1\nx2", "START two", "START three\ny1\nfree", "START four"],
    ),
}


@pytest.mark.parametrize("mode", sorted(MULTILINE_MODES))
def test_multiline_other_modes(pipe, mode):
    start, condition, body, expected = MULTILINE_MODES[mode]
    pipe.put("m/modes.log", body)
    pipe.notify("m/modes.log")
    source = f'multiline.start_pattern = {start}\nmultiline.condition_pattern = {condition}\nmultiline.mode = "{mode}"\nmultiline.timeout_ms = 1000'
    pipe.start(pipe.config(source=source))
    found = pipe.wait_settled(len(expected))
    assert messages_by_object(found) == {"m/modes.log": sorted(expected)}
    assert pipe.stop() == 0


def test_character_delimited_framing(pipe):
    pipe.put("cd.log", b"a;b;;c\nd;e")
    pipe.notify("cd.log")
    pipe.start(pipe.config(source='framing.method = "character_delimited"\nframing.character_delimited.delimiter = ";"'))
    found = pipe.wait_settled(5)
    assert messages_by_object(found) == {"cd.log": sorted(["a", "b", "", "c\nd", "e"])}
    assert pipe.stop() == 0
