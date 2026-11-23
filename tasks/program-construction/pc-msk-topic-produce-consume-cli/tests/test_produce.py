"""Producer mode: splitting input into records, keys, NULLs, headers, files and placement.

Every scenario inspects the topic independently with confluent-kafka.
"""

from harness import workspace, write_fixture


def kv(records):
    return [(r["key"], r["value"]) for r in records]


def test_multibyte_delimiters_with_null_option(kafka):
    # Adapted from upstream tests/0002-delim.sh (partition 0).
    topic = kafka.topic(2)
    data = b"Key1;KeyDel;Value1:MyDilemma::MyDilemma:;KeyDel;Value2:MyDilemma:Key3;KeyDel;:MyDilemma:Value4"
    result = kafka.run("-P", "-t", topic, "-p", "0", "-K", ";KeyDel;", "-D", ":MyDilemma:", "-Z", stdin=data)
    assert result.returncode == 0, result.stderr
    records = kafka.records(topic)
    assert kv(records[0]) == [(b"Key1", b"Value1"), (None, b"Value2"), (b"Key3", None), (None, b"Value4")]
    assert [r["offset"] for r in records[0]] == [0, 1, 2, 3]
    assert records[1] == []
    assert all(r["headers"] == [] for r in records[0])


def test_single_byte_key_delimiter_with_and_without_null_option(kafka):
    # Adapted from upstream tests/0002-delim.sh (partition 1), plus the same input without -Z.
    topic = kafka.topic(2)
    data = b"The First;Message1\n\nIs The;\n;Greatest\nFor sure\n"
    assert kafka.run("-P", "-t", topic, "-p", "0", "-K", ";", "-Z", stdin=data).returncode == 0
    assert kafka.run("-P", "-t", topic, "-p", "1", "-K", ";", stdin=data).returncode == 0
    records = kafka.records(topic)
    # The empty line produces nothing; a line without the delimiter has a NULL key either way.
    assert kv(records[0]) == [(b"The First", b"Message1"), (b"Is The", None), (None, b"Greatest"), (None, b"For sure")]
    assert kv(records[1]) == [(b"The First", b"Message1"), (b"Is The", b""), (b"", b"Greatest"), (None, b"For sure")]


def test_plain_lines_null_option_and_unterminated_last_line(kafka):
    topic = kafka.topic()
    result = kafka.run("-P", "-t", topic, "-Z", stdin=b"a\n\n\nb\nlast without newline")
    assert result.returncode == 0, result.stderr
    assert kv(kafka.records(topic)[0]) == [(None, b"a"), (None, b"b"), (None, b"last without newline")]


def test_fixed_key_and_key_delimiter_precedence(kafka):
    topic = kafka.topic()
    result = kafka.run("-P", "-t", topic, "-K", ":", "-k", "FIX", stdin=b"a:b:c\nnokey\n:\nk:\n")
    assert result.returncode == 0, result.stderr
    # The first delimiter splits; the fixed key only fills in when no delimiter is present.
    assert kv(kafka.records(topic)[0]) == [(b"a", b"b:c"), (b"FIX", b"nokey"), (b"", b""), (b"k", b"")]


def test_fixed_key_replaces_null_key_from_empty_prefix(kafka):
    topic = kafka.topic()
    result = kafka.run("-P", "-t", topic, "-K", ":", "-k", "FK", "-Z", stdin=b":v\nx:\n")
    assert result.returncode == 0, result.stderr
    assert kv(kafka.records(topic)[0]) == [(b"FK", b"v"), (b"x", None)]


def test_fixed_key_without_key_delimiter(kafka):
    topic = kafka.topic()
    assert kafka.run("-P", "-t", topic, "-k", "same", stdin=b"one\ntwo:2\n").returncode == 0
    assert kv(kafka.records(topic)[0]) == [(b"same", b"one"), (b"same", b"two:2")]


def test_delimiter_escapes(kafka):
    topic = kafka.topic(3)
    assert kafka.run("-P", "-t", topic, "-p", "0", "-K", "\\t", "-D", "\\x1e", stdin=b"k\tv1\x1ek2\tv2\x1e").returncode == 0
    assert kafka.run("-P", "-t", topic, "-p", "1", "-D", "\\x0a", stdin=b"m1\nm2").returncode == 0
    # A backslash-r delimiter argument is not translated into a carriage return.
    assert kafka.run("-P", "-t", topic, "-p", "2", "-K", "\\r", stdin=b"k\rv\na\\rb\n").returncode == 0
    records = kafka.records(topic)
    assert kv(records[0]) == [(b"k", b"v1"), (b"k2", b"v2")]
    assert kv(records[1]) == [(None, b"m1"), (None, b"m2")]
    assert kv(records[2]) == [(None, b"k\rv"), (b"a", b"b")]


def test_binary_safe_payloads(kafka):
    topic = kafka.topic()
    data = b'q"uo\\te\tTAB\x01ctl\xc3\xa9|k\x00z|\xff\xfe\n'
    assert kafka.run("-P", "-t", topic, "-D", "|", stdin=data).returncode == 0
    assert kv(kafka.records(topic)[0]) == [(None, b'q"uo\\te\tTAB\x01ctl\xc3\xa9'), (None, b"k\x00z"), (None, b"\xff\xfe\n")]


def test_long_messages_with_delimiters_across_read_boundaries(kafka):
    topic = kafka.topic()
    parts = [(b"k%d" % i, b"v%d-" % i + b"x" * (97 * i + 1000 * (i % 3))) for i in range(40)]
    data = b":MyDilemma:".join(k + b";KeyDel;" + v for k, v in parts)
    result = kafka.run("-P", "-t", topic, "-K", ";KeyDel;", "-D", ":MyDilemma:", stdin=data)
    assert result.returncode == 0, result.stderr
    assert kv(kafka.records(topic)[0]) == parts


def test_headers_order_bare_empty_and_duplicates(kafka):
    topic = kafka.topic()
    args = ["-H", "a=1", "-H", "bare", "-H", "empty=", "-H", "a=2", "-H", "eq=x=y"]
    result = kafka.run("-P", "-t", topic, "-K", ":", *args, stdin=b"k:h1\nh2\n")
    assert result.returncode == 0, result.stderr
    expected = [("a", b"1"), ("bare", None), ("empty", b""), ("a", b"2"), ("eq", b"x=y")]
    records = kafka.records(topic)[0]
    assert kv(records) == [(b"k", b"h1"), (None, b"h2")]
    assert [r["headers"] for r in records] == [expected, expected]


def test_header_without_name_is_rejected(kafka):
    topic = kafka.topic()
    result = kafka.run("-P", "-t", topic, "-H", "=x", stdin=b"m\n")
    assert result.returncode == 1
    assert kafka.records(topic)[0] == []


def test_message_count_limit_and_tee(kafka):
    topic = kafka.topic()
    result = kafka.run("-P", "-t", topic, "-T", "-K", ":", "-c", "3", stdin=b"x1\nk:x2\nx3\nx4\n")
    assert result.returncode == 0, result.stderr
    assert kv(kafka.records(topic)[0]) == [(None, b"x1"), (b"k", b"x2"), (None, b"x3")]
    # Echoed input is the raw message text, key included, without delimiters.
    assert result.stdout == b"x1k:x2x3"


def test_zero_count_means_unlimited(kafka):
    topic = kafka.topic()
    result = kafka.run("-P", "-t", topic, "-c", "0", stdin=b"c1\nc2\nc3\n")
    assert result.returncode == 0, result.stderr
    assert kv(kafka.records(topic)[0]) == [(None, b"c1"), (None, b"c2"), (None, b"c3")]
    assert result.stdout == b""


def test_file_operands_are_whole_messages(kafka):
    topic = kafka.topic()
    with workspace() as root:
        write_fixture(root / "f1", b"line1\nline2\n")
        write_fixture(root / "empty", b"")
        write_fixture(root / "f3", b"second")
        result = kafka.run("-P", "-t", topic, "-k", "fk", "f1", "empty", "f3", "missing", cwd=root)
    # One unreadable file among readable ones still succeeds; the empty file sends nothing.
    assert result.returncode == 0, result.stderr
    assert kv(kafka.records(topic)[0]) == [(b"fk", b"line1\nline2\n"), (b"fk", b"second")]


def test_all_file_operands_unreadable(kafka):
    topic = kafka.topic()
    with workspace() as root:
        result = kafka.run("-P", "-t", topic, "missing1", "missing2", cwd=root)
    assert result.returncode == 1
    assert kafka.records(topic)[0] == []


def test_line_mode_file_with_tee_and_fixed_key(kafka):
    topic = kafka.topic()
    with workspace() as root:
        write_fixture(root / "in.txt", b"l1\nl2\n")
        write_fixture(root / "other.txt", b"o\n")
        result = kafka.run("-P", "-t", topic, "-l", "in.txt", "-T", "-k", "K", stdin=b"from-stdin\n", cwd=root)
        assert result.returncode == 0, result.stderr
        assert result.stdout == b"l1l2"
        assert kv(kafka.records(topic)[0]) == [(b"K", b"l1"), (b"K", b"l2")]
        refused = kafka.run("-P", "-t", topic, "-l", "in.txt", "other.txt", cwd=root)
    assert refused.returncode == 1
    assert kv(kafka.records(topic)[0]) == [(b"K", b"l1"), (b"K", b"l2")]


def test_explicit_partitions(kafka):
    topic = kafka.topic(3)
    assert kafka.run("-P", "-t", topic, "-p", "2", stdin=b"two-a\ntwo-b\n").returncode == 0
    assert kafka.run("-P", "-t", topic, "-p", "0", stdin=b"zero\n").returncode == 0
    records = kafka.records(topic)
    assert {p: kv(r) for p, r in records.items()} == {0: [(None, b"zero")], 1: [], 2: [(None, b"two-a"), (None, b"two-b")]}


def test_unknown_partition_fails_delivery(kafka):
    topic = kafka.topic()
    result = kafka.run("-P", "-t", topic, "-p", "7", stdin=b"m\n")
    assert result.returncode == 1
    assert kafka.records(topic)[0] == []


def test_keyed_messages_use_default_partitioner(kafka):
    topic = kafka.topic(3)
    data = b"alpha:1\nbeta:2\ngamma:3\ndelta:4\nalpha:5\nepsilon:6\nzeta:7\n"
    assert kafka.run("-P", "-t", topic, "-K", ":", stdin=data).returncode == 0
    assert kafka.run("-P", "-t", topic, "-K", ":", "-p", "-1", stdin=b"zeta:8\ngamma:9\n").returncode == 0
    records = kafka.records(topic)
    assert {p: kv(r) for p, r in records.items()} == {
        0: [(b"zeta", b"7"), (b"zeta", b"8")],
        1: [(b"alpha", b"1"), (b"beta", b"2"), (b"delta", b"4"), (b"alpha", b"5"), (b"epsilon", b"6")],
        2: [(b"gamma", b"3"), (b"gamma", b"9")],
    }


def test_compression_codecs_deliver_records(kafka):
    topic = kafka.topic()
    for codec in ("gzip", "snappy", "lz4", "zstd", "none"):
        result = kafka.run("-P", "-t", topic, "-z", codec, "-K", ":", stdin=b"%s:payload-%s\n" % (codec.encode(), codec.encode()))
        assert result.returncode == 0, (codec, result.stderr)
    refused = kafka.run("-P", "-t", topic, "-z", "bogus", stdin=b"never\n")
    assert refused.returncode == 1
    assert kv(kafka.records(topic)[0]) == [(c.encode(), b"payload-" + c.encode()) for c in ("gzip", "snappy", "lz4", "zstd", "none")]


def test_empty_message_delimiter_is_rejected(kafka):
    topic = kafka.topic()
    result = kafka.run("-P", "-t", topic, "-D", "", stdin=b"m\n")
    assert result.returncode == 1
    assert kafka.records(topic)[0] == []
