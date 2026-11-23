"""Single-instance consumption: emitted messages, checkpoint rows, resume points and acknowledgement rules."""

import signal
import time

import pytest

import fixture_io
from conftest import (
    KINESIS,
    as_multiset,
    assert_live,
    assert_string_fields,
    candidate_dir,
    consumer_config,
    final_row,
    owner,
    parse_time,
    scan,
    stamp,
    wait_for,
)


def rows_with_sequences(table, expected):
    """Wait condition: every (stream, shard) -> sequence in `expected` is stored."""

    def check():
        rows = scan(table) or {}
        if all(key in rows and rows[key].get("SequenceNumber", (None, None))[1] == seq for key, seq in expected.items()):
            return rows
        return None

    return check


def test_balanced_lifecycle_rows_and_messages(lab):
    stream, shards = lab.stream(2)
    table = lab.table()
    sent = [lab.put(stream, f"a{i}", f"pk-a{i}", 0, 2, shards[0]) for i in range(3)]
    sent += [lab.put(stream, f"b{i}", f"pk-b{i}", 1, 2, shards[1]) for i in range(2)]
    lab.write_config("c.yaml", consumer_config([stream], table))
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 5, 30, "five messages")
    last = {(stream, shards[0]): sent[2]["sequence"], (stream, shards[1]): sent[4]["sequence"]}
    rows = wait_for(rows_with_sequences(table, last), 15, "live checkpoints of both shards")
    assert set(rows) == set(last)
    clients = {assert_live(rows[key], stream, key[1], seq, 5) for key, seq in last.items()}
    assert len(clients) == 1, "one instance leases every shard under one ClientID"
    # The lease is renewed at every commit.
    first = parse_time(rows[(stream, shards[0])]["LeaseTimeout"][1])
    time.sleep(2.5)
    renewed = scan(table)
    assert parse_time(renewed[(stream, shards[0])]["LeaseTimeout"][1]) > first
    assert_live(renewed[(stream, shards[0])], stream, shards[0], sent[2]["sequence"], 5)
    assert owner(renewed[(stream, shards[1])]) in clients

    assert instance.stop() == 0
    messages = lab.lines("out.jsonl")
    assert_string_fields(messages)
    assert as_multiset(messages) == as_multiset(sent)
    assert scan(table) == {key: final_row(*key, seq) for key, seq in last.items()}


def test_never_consumed_shard_rows_and_later_reclaim(lab):
    stream, shards = lab.stream(2)
    table = lab.table()
    record = lab.put(stream, "only", "pk", 0, 2, shards[0])
    lab.write_config("one.yaml", consumer_config([stream], table, out="one.jsonl"))
    lab.write_config("two.yaml", consumer_config([stream], table, out="two.jsonl"))
    instance = lab.start("one.yaml")
    rows = wait_for(rows_with_sequences(table, {(stream, shards[0]): record["sequence"]}), 30, "shard 0 checkpoint")
    rows = wait_for(lambda: (r := scan(table)) and (stream, shards[1]) in r and r, 10, "a lease row for the empty shard")
    client = assert_live(rows[(stream, shards[1])], stream, shards[1], None, 5)
    assert client == owner(rows[(stream, shards[0])])
    assert instance.stop() == 0
    assert lab.lines("one.jsonl") == [record]
    assert scan(table) == {
        (stream, shards[0]): final_row(stream, shards[0], record["sequence"]),
        (stream, shards[1]): final_row(stream, shards[1]),
    }
    # A released row without a sequence is claimed again and read from the oldest record.
    backlog = [lab.put(stream, f"later{i}", "pk", 1, 2, shards[1]) for i in range(3)]
    again = lab.start("two.yaml")
    wait_for(lambda: len(lab.lines("two.jsonl")) >= 3, 30, "the idle shard's backlog")
    rows = wait_for(rows_with_sequences(table, {(stream, shards[1]): backlog[2]["sequence"]}), 15, "checkpoint on the reclaimed shard")
    second = assert_live(rows[(stream, shards[1])], stream, shards[1], backlog[2]["sequence"], 5)
    assert second != client
    assert assert_live(rows[(stream, shards[0])], stream, shards[0], record["sequence"], 5) == second
    time.sleep(1.5)
    assert again.stop() == 0
    assert as_multiset(lab.lines("two.jsonl")) == as_multiset(backlog)
    assert scan(table) == {
        (stream, shards[0]): final_row(stream, shards[0], record["sequence"]),
        (stream, shards[1]): final_row(stream, shards[1], backlog[2]["sequence"]),
    }


def test_resume_after_seeded_checkpoint_leaves_other_rows(lab):
    stream, shards = lab.stream(2)
    table = lab.table(create=True)
    first = [lab.put(stream, f"a{i}", f"pk{i}", 0, 2, shards[0]) for i in range(3)]
    other = lab.put(stream, "b0", "pkb", 1, 2, shards[1])
    lab.seed(table, StreamID=stream, ShardID=shards[0], SequenceNumber=first[0]["sequence"])
    sentinel = {"StreamID": f"unrelated-{lab.tag}", "ShardID": shards[0], "SequenceNumber": "42", "ClientID": "someone-else", "LeaseTimeout": stamp(-3600)}
    lab.seed(table, **sentinel)
    lab.write_config("c.yaml", consumer_config([stream], table))
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 3, 30, "three messages")
    expected = {(stream, shards[0]): first[2]["sequence"], (stream, shards[1]): other["sequence"]}
    wait_for(rows_with_sequences(table, expected), 15, "advanced checkpoints")
    assert instance.stop() == 0
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset([first[1], first[2], other])
    rows = scan(table)
    assert rows.pop((sentinel["StreamID"], shards[0])) == {k: ("S", v) for k, v in sentinel.items()}
    assert rows == {key: final_row(*key, seq) for key, seq in expected.items()}


def test_restart_emits_only_new_records(lab):
    stream, shards = lab.stream(2)
    table = lab.table()
    before = [lab.put(stream, f"old{i}", f"k{i}", i % 2, 2) for i in range(4)]
    lab.write_config("one.yaml", consumer_config([stream], table, out="one.jsonl"))
    lab.write_config("two.yaml", consumer_config([stream], table, out="two.jsonl"))
    first = lab.start("one.yaml")
    wait_for(lambda: len(lab.lines("one.jsonl")) >= 4, 30, "first run messages")
    time.sleep(1.5)
    assert first.stop(sig=signal.SIGINT) == 0
    after = [lab.put(stream, f"new{i}", f"k{i}", i % 2, 2) for i in range(3)]
    second = lab.start("two.yaml")
    wait_for(lambda: len(lab.lines("two.jsonl")) >= 3, 30, "second run messages")
    time.sleep(2)
    assert second.stop() == 0
    assert as_multiset(lab.lines("one.jsonl")) == as_multiset(before)
    assert as_multiset(lab.lines("two.jsonl")) == as_multiset(after)
    newest = {}
    for record in before + after:
        newest[(stream, record["shard"])] = record["sequence"]
    assert scan(table) == {key: final_row(*key, seq) for key, seq in newest.items()}


def test_start_from_latest_skips_backlog_but_not_stored_checkpoints(lab):
    stream, shards = lab.stream(2)
    table = lab.table(create=True)
    stored = lab.put(stream, "a0", "pk", 0, 2, shards[0])
    after_stored = lab.put(stream, "a1", "pk", 0, 2, shards[0])
    lab.put(stream, "backlog", "pk", 1, 2, shards[1])
    lab.seed(table, StreamID=stream, ShardID=shards[0], SequenceNumber=stored["sequence"])
    lab.write_config("c.yaml", consumer_config([stream], table, start_from_oldest=False))
    instance = lab.start("c.yaml")
    wait_for(lambda: all("ClientID" in row for row in (scan(table) or {}).values()) and len(scan(table)) == 2, 20, "both shards leased")
    time.sleep(2)
    fresh = [lab.put(stream, "a2", "pk", 0, 2, shards[0]), lab.put(stream, "b1", "pk", 1, 2, shards[1])]
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 3, 20, "three messages")
    time.sleep(2)
    assert instance.stop() == 0
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset([after_stored, *fresh])
    assert scan(table) == {(stream, shards[0]): final_row(stream, shards[0], fresh[0]["sequence"]), (stream, shards[1]): final_row(stream, shards[1], fresh[1]["sequence"])}


def test_explicit_shard_consumes_only_that_shard(lab):
    stream, shards = lab.stream(2)
    table = lab.table()
    lab.put(stream, "zero", "pk0", 0, 2, shards[0])
    wanted = [lab.put(stream, f"one{i}", "pk1", 1, 2, shards[1]) for i in range(2)]
    lab.write_config("c.yaml", consumer_config([f"{stream}:{shards[1]}"], table))
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 2, 30, "shard 1 messages")
    rows = wait_for(rows_with_sequences(table, {(stream, shards[1]): wanted[1]["sequence"]}), 15, "shard 1 checkpoint")
    assert set(rows) == {(stream, shards[1])}
    assert_live(rows[(stream, shards[1])], stream, shards[1], wanted[1]["sequence"], 5)
    time.sleep(2)
    assert instance.stop() == 0
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset(wanted)
    assert scan(table) == {(stream, shards[1]): final_row(stream, shards[1], wanted[1]["sequence"])}


def test_explicit_shards_never_take_an_owned_row(lab):
    stream, shards = lab.stream(2)
    table = lab.table(create=True)
    old = [lab.put(stream, f"old{i}", "pk", i, 2, shards[i]) for i in range(2)]
    new = [lab.put(stream, f"new{i}", "pk", i, 2, shards[i]) for i in range(2)]
    held = {"StreamID": stream, "ShardID": shards[0], "ClientID": "long-gone", "LeaseTimeout": stamp(-1000), "SequenceNumber": old[0]["sequence"]}
    lab.seed(table, **held)
    lab.seed(table, StreamID=stream, ShardID=shards[1], SequenceNumber=old[1]["sequence"])
    lab.write_config("c.yaml", consumer_config([f"{stream}:{shards[0]},{stream}:{shards[1]}"], table, lease_period="2s"))
    instance = lab.start("c.yaml")
    wait_for(lambda: lab.lines("out.jsonl"), 30, "shard 1 message")
    time.sleep(7)  # several lease periods: balanced mode would have taken the expired row by now
    assert instance.running()
    rows = scan(table)
    assert rows[(stream, shards[0])] == {k: ("S", v) for k, v in held.items()}
    assert_live(rows[(stream, shards[1])], stream, shards[1], new[1]["sequence"], 2, slack=2.5)
    assert instance.stop() == 0
    assert lab.lines("out.jsonl") == [new[1]]
    assert scan(table) == {(stream, shards[0]): {k: ("S", v) for k, v in held.items()}, (stream, shards[1]): final_row(stream, shards[1], new[1]["sequence"])}


def test_comma_separated_streams_are_balanced_independently(lab):
    first, first_shards = lab.stream(1, "one")
    second, second_shards = lab.stream(2, "two")
    table = lab.table()
    sent = [lab.put(first, "x", "p1"), lab.put(second, "y0", "p2", 0, 2), lab.put(second, "y1", "p3", 1, 2)]
    lab.write_config("c.yaml", consumer_config([f"{first}, {second}"], table))
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 3, 30, "three messages")
    expected = {(r["stream"], r["shard"]): r["sequence"] for r in sent}
    wait_for(rows_with_sequences(table, expected), 15, "checkpoints for both streams")
    assert instance.stop() == 0
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset(sent)
    assert scan(table) == {key: final_row(*key, seq) for key, seq in expected.items()}


@pytest.mark.parametrize("mode", ["balanced", "explicit"])
def test_stream_arn_is_the_stream_identity(lab, mode):
    stream, shards = lab.stream(2)
    table = lab.table()
    arn = KINESIS.describe_stream_summary(StreamName=stream)["StreamDescriptionSummary"]["StreamARN"]
    sent = [lab.put(stream, f"r{i}", f"pk{i}", i, 2, shards[i]) for i in range(2)]
    if mode == "explicit":
        sent = sent[1:]
    target = arn if mode == "balanced" else f"{arn}:{shards[1]}"
    lab.write_config("c.yaml", consumer_config([target], table))
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= len(sent), 30, "messages")
    expected = {(arn, r["shard"]): r["sequence"] for r in sent}
    wait_for(rows_with_sequences(table, expected), 15, "checkpoints keyed by the ARN")
    assert instance.stop() == 0
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset({**r, "stream": arn} for r in sent)
    assert scan(table) == {key: final_row(*key, seq) for key, seq in expected.items()}


def test_commit_period_defers_checkpoints_until_shutdown(lab):
    stream, shards = lab.stream(1)
    table = lab.table()
    sent = [lab.put(stream, f"r{i}", "pk") for i in range(3)]
    lab.write_config("c.yaml", consumer_config([stream], table, commit_period="30s", lease_period="20s"))
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 3, 30, "three messages")
    time.sleep(4)
    rows = scan(table)
    assert set(rows) == {(stream, shards[0])}
    # Still only the claim: no sequence stored yet and the lease not renewed since claiming.
    assert_live(rows[(stream, shards[0])], stream, shards[0], None, 20, slack=10)
    assert instance.stop() == 0
    assert scan(table) == {(stream, shards[0]): final_row(stream, shards[0], sent[2]["sequence"])}


def test_input_batch_count_holds_a_partial_batch(lab):
    stream, shards = lab.stream(1)
    table = lab.table()
    sent = [lab.put(stream, f"r{i}", "pk") for i in range(7)]
    lab.write_config("count.yaml", consumer_config([stream], table, out="count.jsonl", batching={"count": 3}))
    lab.write_config("period.yaml", consumer_config([stream], table, out="period.jsonl", batching={"count": 3, "period": "500ms"}))
    first = lab.start("count.yaml")
    wait_for(lambda: len(lab.lines("count.jsonl")) >= 6, 30, "two full batches")
    wait_for(rows_with_sequences(table, {(stream, shards[0]): sent[5]["sequence"]}), 15, "checkpoint at the sixth record")
    time.sleep(3)
    assert as_multiset(lab.lines("count.jsonl")) == as_multiset(sent[:6])
    assert scan(table)[(stream, shards[0])]["SequenceNumber"] == ("S", sent[5]["sequence"])
    assert first.stop() == 0
    assert scan(table) == {(stream, shards[0]): final_row(stream, shards[0], sent[5]["sequence"])}
    second = lab.start("period.yaml")
    wait_for(lambda: lab.lines("period.jsonl"), 20, "the held record after a timed flush")
    time.sleep(2)
    assert second.stop() == 0
    assert lab.lines("period.jsonl") == [sent[6]]
    assert scan(table) == {(stream, shards[0]): final_row(stream, shards[0], sent[6]["sequence"])}


# ------------------------------------------------------------ acknowledgement gaps
# One partition key's output path is blocked by a regular file, so its message is rejected
# by the output until the test removes the blocker.


def _gap_setup(lab, **fields):
    stream, shards = lab.stream(1)
    table = lab.table()
    sent = [lab.put(stream, f"r{i}", key) for i, key in enumerate(["ok", "bad", "ok", "ok"])]
    out = candidate_dir(lab, "out")
    fixture_io.write_text(out / "bad", "blocker\n")
    path = 'out/${! meta("kinesis_partition_key") }/records.jsonl'
    lab.write_config("c.yaml", consumer_config([stream], table, output={"file": {"path": path, "codec": "lines"}}, **fields))
    return stream, shards[0], table, sent, out


def test_checkpoint_stops_at_the_first_unacknowledged_record(lab):
    stream, shard, table, sent, out = _gap_setup(lab)
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out/ok/records.jsonl")) >= 3, 30, "the deliverable messages")
    wait_for(rows_with_sequences(table, {(stream, shard): sent[0]["sequence"]}), 15, "checkpoint at r0")
    time.sleep(3)
    assert scan(table)[(stream, shard)]["SequenceNumber"] == ("S", sent[0]["sequence"])
    assert as_multiset(lab.lines("out/ok/records.jsonl")) == as_multiset([sent[0], sent[2], sent[3]])
    fixture_io.unlink(out / "bad")
    wait_for(lambda: lab.lines("out/bad/records.jsonl"), 25, "the retried message")
    wait_for(rows_with_sequences(table, {(stream, shard): sent[3]["sequence"]}), 15, "checkpoint at r3 once r1 is delivered")
    assert instance.stop() == 0
    assert lab.lines("out/bad/records.jsonl") == [sent[1]]
    assert as_multiset(lab.lines("out/ok/records.jsonl")) == as_multiset([sent[0], sent[2], sent[3]])
    assert scan(table) == {(stream, shard): final_row(stream, shard, sent[3]["sequence"])}


def test_checkpoint_limit_bounds_messages_in_flight(lab):
    stream, shard, table, sent, out = _gap_setup(lab, checkpoint_limit=1)
    instance = lab.start("c.yaml")
    wait_for(lambda: lab.lines("out/ok/records.jsonl"), 30, "r0")
    wait_for(rows_with_sequences(table, {(stream, shard): sent[0]["sequence"]}), 15, "checkpoint at r0")
    time.sleep(4)
    assert lab.lines("out/ok/records.jsonl") == [sent[0]], "with one message in flight nothing passes the rejected r1"
    fixture_io.unlink(out / "bad")
    wait_for(lambda: len(lab.lines("out/ok/records.jsonl")) >= 3, 25, "the remaining messages")
    wait_for(rows_with_sequences(table, {(stream, shard): sent[3]["sequence"]}), 15, "checkpoint at r3")
    assert instance.stop() == 0
    assert lab.lines("out/bad/records.jsonl") == [sent[1]]
    assert as_multiset(lab.lines("out/ok/records.jsonl")) == as_multiset([sent[0], sent[2], sent[3]])
    assert scan(table) == {(stream, shard): final_row(stream, shard, sent[3]["sequence"])}


def test_rejected_messages_are_dropped_without_auto_replay(lab):
    stream, shard, table, sent, out = _gap_setup(lab, auto_replay_nacks=False)
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out/ok/records.jsonl")) >= 3, 30, "the deliverable messages")
    wait_for(rows_with_sequences(table, {(stream, shard): sent[3]["sequence"]}), 15, "checkpoint past the dropped r1")
    fixture_io.unlink(out / "bad")
    time.sleep(4)
    assert instance.stop() == 0
    assert not (out / "bad").exists(), "a dropped message is never retried"
    assert as_multiset(lab.lines("out/ok/records.jsonl")) == as_multiset([sent[0], sent[2], sent[3]])
    assert scan(table) == {(stream, shard): final_row(stream, shard, sent[3]["sequence"])}
