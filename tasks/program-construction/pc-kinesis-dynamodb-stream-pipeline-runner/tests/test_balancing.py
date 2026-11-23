"""Lease protocol against other clients: stealing, yielding, expiry takeover, crashes and resharding.

Most scenarios seed rows for a fictitious client ("ghost") so the program's decisions are observed
in isolation; the multi-process interactions live in test_workflows.py.
"""

import time
from datetime import timedelta

import pytest

from conftest import (
    DYNAMO,
    KINESIS,
    as_multiset,
    assert_live,
    consumer_config,
    final_row,
    now,
    owner,
    parse_time,
    scan,
    stamp,
    wait_for,
)


def as_row(attributes):
    return {k: ("S", v) for k, v in attributes.items()}


def seed_ghost(lab, held, lease_offset):
    stream, shards = lab.stream(held)
    table = lab.table(create=True)
    ghost = {}
    for i, shard in enumerate(shards):
        old = lab.put(stream, f"old{i}", f"pk{i}", i, held, shard)
        ghost[shard] = {"StreamID": stream, "ShardID": shard, "ClientID": "ghost", "LeaseTimeout": stamp(lease_offset), "SequenceNumber": old["sequence"]}
        lab.seed(table, **ghost[shard])
    return stream, shards, table, ghost


@pytest.mark.parametrize(("held", "stolen"), [(1, 0), (2, 1), (3, 1), (4, 2), (5, 2)])
def test_steals_while_other_client_holds_two_more(lab, held, stolen):
    # Live (unexpired) leases held by another client: the newcomer steals one shard per
    # rebalance while the other client holds more than its own count + 1.
    stream, shards, table, ghost = seed_ghost(lab, held, 30)
    lab.write_config("c.yaml", consumer_config([stream], table, lease_period="30s"))
    instance = lab.start("c.yaml")

    def taken():
        rows = scan(table)
        return [s for s in shards if owner(rows[(stream, s)]) != "ghost"]

    if stolen:
        wait_for(lambda: len(taken()) >= stolen, 25, f"{stolen} stolen shard(s)")
    time.sleep(5)
    rows = scan(table)
    mine = taken()
    assert len(mine) == stolen, rows
    for shard in shards:
        if shard not in mine:
            assert rows[(stream, shard)] == as_row(ghost[shard]), "rows left to the other client are untouched"
    assert len({owner(rows[(stream, s)]) for s in mine}) <= 1
    for shard in mine:
        assert_live(rows[(stream, shard)], stream, shard, ghost[shard]["SequenceNumber"], 30)

    new = {shard: lab.put(stream, f"new{i}", f"pk{i}", i, held, shard) for i, shard in enumerate(shards)}
    if stolen:
        wait_for(lambda: len(lab.lines("out.jsonl")) >= stolen, 15, "messages from stolen shards")
    time.sleep(2)
    assert instance.stop() == 0
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset(new[s] for s in mine)
    expected = {(stream, s): final_row(stream, s, new[s]["sequence"]) if s in mine else as_row(ghost[s]) for s in shards}
    assert scan(table) == expected


def test_theft_rereads_the_sequence_after_the_grace_wait(lab):
    stream, shards = lab.stream(2)
    table = lab.table(create=True)
    records = {s: [lab.put(stream, f"{w}{i}", f"pk{i}", i, 2, s) for w in ("old", "mid", "new")] for i, s in enumerate(shards)}
    seeded = time.monotonic()
    # The ghost's last checkpoint appears to be 4s in the future, so a thief must wait about
    # 4s + commit_period + steal_grace_period before reading the sequence it resumes from.
    seeded_rows = {}
    for shard in shards:
        seeded_rows[shard] = {"StreamID": stream, "ShardID": shard, "ClientID": "ghost", "LeaseTimeout": stamp(34), "SequenceNumber": records[shard][0]["sequence"]}
        lab.seed(table, **seeded_rows[shard])
    lab.write_config("c.yaml", consumer_config([stream], table, lease_period="30s"))
    instance = lab.start("c.yaml")
    stolen = wait_for(lambda: [key[1] for key, row in scan(table).items() if owner(row) != "ghost"], 15, "a claimed shard", interval=0.05)
    assert len(stolen) == 1
    shard = stolen[0]
    # The victim "checkpoints" mid while the thief waits.
    DYNAMO.update_item(
        TableName=table,
        Key={"StreamID": {"S": stream}, "ShardID": {"S": shard}},
        UpdateExpression="SET SequenceNumber = :s",
        ExpressionAttributeValues={":s": {"S": records[shard][1]["sequence"]}},
    )
    assert lab.lines("out.jsonl") == []
    wait_for(lambda: lab.lines("out.jsonl"), 20, "the first message after the theft")
    assert time.monotonic() - seeded >= 4.5, "the thief waits for the victim's next checkpoint"
    time.sleep(3)
    assert instance.stop() == 0
    assert lab.lines("out.jsonl") == [records[shard][2]]
    other = next(s for s in shards if s != shard)
    rows = scan(table)
    assert rows[(stream, shard)] == final_row(stream, shard, records[shard][2]["sequence"])
    assert rows[(stream, other)] == as_row(seeded_rows[other])


def test_victim_yields_a_stolen_shard(lab):
    stream, shards = lab.stream(2)
    table = lab.table()
    a = [lab.put(stream, f"a{i}", "pa", 0, 2, shards[0]) for i in range(2)]
    b = [lab.put(stream, "b0", "pb", 1, 2, shards[1])]
    lab.write_config("c.yaml", consumer_config([stream], table))
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 3, 30, "initial messages")
    wait_for(lambda: all("SequenceNumber" in r for r in (scan(table) or {}).values()) and len(scan(table)) == 2, 15, "both checkpoints")
    a.append(lab.put(stream, "a2", "pa", 0, 2, shards[0]))
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 4, 15, "a2")
    # Another client takes shard 0 and rewinds its sequence.
    thief_lease = stamp(60)
    DYNAMO.update_item(
        TableName=table,
        Key={"StreamID": {"S": stream}, "ShardID": {"S": shards[0]}},
        UpdateExpression="SET ClientID = :c, LeaseTimeout = :l, SequenceNumber = :s",
        ExpressionAttributeValues={":c": {"S": "thief"}, ":l": {"S": thief_lease}, ":s": {"S": a[0]["sequence"]}},
    )
    stolen_row = {"StreamID": stream, "ShardID": shards[0], "ClientID": "thief", "LeaseTimeout": thief_lease, "SequenceNumber": a[2]["sequence"]}
    wait_for(lambda: scan(table)[(stream, shards[0])] == as_row(stolen_row), 10, "the victim to hand over its acknowledged sequence")
    late = lab.put(stream, "a3", "pa", 0, 2, shards[0])
    b.append(lab.put(stream, "b1", "pb", 1, 2, shards[1]))
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 5, 15, "b1")
    time.sleep(3)
    rows = scan(table)
    assert rows[(stream, shards[0])] == as_row(stolen_row)
    assert_live(rows[(stream, shards[1])], stream, shards[1], b[1]["sequence"], 5)
    assert instance.stop() == 0
    assert late["data"] not in [m["data"] for m in lab.lines("out.jsonl")]
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset(a + b)
    assert scan(table) == {(stream, shards[0]): as_row(stolen_row), (stream, shards[1]): final_row(stream, shards[1], b[1]["sequence"])}


def test_expired_lease_is_taken_only_after_twice_the_lease_period(lab):
    stream, shards = lab.stream(2)
    table = lab.table(create=True)
    old = [lab.put(stream, f"old{i}", "pk", i, 2, shards[i]) for i in range(2)]
    new = [lab.put(stream, f"new{i}", "pk", i, 2, shards[i]) for i in range(2)]
    seeded = time.monotonic()
    lab.seed(table, StreamID=stream, ShardID=shards[0], ClientID="ghost", LeaseTimeout=stamp(-11), SequenceNumber=old[0]["sequence"])
    recent = {"StreamID": stream, "ShardID": shards[1], "ClientID": "ghost", "LeaseTimeout": stamp(-3), "SequenceNumber": old[1]["sequence"]}
    lab.seed(table, **recent)
    lab.write_config("c.yaml", consumer_config([stream], table, lease_period="5s"))
    instance = lab.start("c.yaml")
    wait_for(lambda: lab.lines("out.jsonl"), 15, "the long-expired shard to be taken over")
    assert lab.lines("out.jsonl") == [new[0]]
    time.sleep(max(0.0, 5.5 - (time.monotonic() - seeded)))
    assert scan(table)[(stream, shards[1])] == as_row(recent), "expired less than two lease periods ago: not yet taken"
    assert lab.lines("out.jsonl") == [new[0]]
    wait_for(lambda: owner(scan(table)[(stream, shards[1])]) not in (None, "ghost"), 15, "the second takeover")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 2, 10, "new1")
    rows = scan(table)
    assert owner(rows[(stream, shards[0])]) == owner(rows[(stream, shards[1])])
    time.sleep(1.5)
    assert instance.stop() == 0
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset(new)
    assert scan(table) == {(stream, s): final_row(stream, s, new[i]["sequence"]) for i, s in enumerate(shards)}


def test_crashed_instance_shards_are_stolen_then_taken_over(lab):
    stream, shards = lab.stream(2)
    table = lab.table()
    first = [lab.put(stream, f"a{i}", "pk", i, 2, shards[i]) for i in range(2)]
    lab.write_config("a.yaml", consumer_config([stream], table, out="a.jsonl"))
    lab.write_config("b.yaml", consumer_config([stream], table, out="b.jsonl"))
    crashed = lab.start("a.yaml")
    wait_for(lambda: len(lab.lines("a.jsonl")) >= 2, 30, "first instance messages")
    wait_for(lambda: all("SequenceNumber" in r for r in (scan(table) or {}).values()) and len(scan(table)) == 2, 15, "checkpoints")
    crashed.kill()
    left = scan(table)
    dead = owner(left[(stream, shards[0])])
    assert all(set(r) == {"StreamID", "ShardID", "ClientID", "LeaseTimeout", "SequenceNumber"} and owner(r) == dead for r in left.values()), left
    later = {s: lab.put(stream, f"b{i}", "pk", i, 2, s) for i, s in enumerate(shards)}
    survivor = lab.start("b.yaml")
    # The crashed client still appears to hold two shards to the newcomer's zero: one is stolen
    # right away; the other is only taken once its lease expired two lease periods ago.
    wait_for(lambda: sum(owner(r) != dead for r in scan(table).values()) >= 1, 10, "one stolen shard")
    stolen = [key[1] for key, r in scan(table).items() if owner(r) != dead]
    assert len(stolen) == 1
    remaining = next(s for s in shards if s not in stolen)
    wait_for(lambda: lab.lines("b.jsonl"), 10, "the stolen shard's message")
    expiry = parse_time(left[(stream, remaining)]["LeaseTimeout"][1])
    time.sleep(max(0.0, (expiry - now()).total_seconds() + 8.0))
    assert scan(table)[(stream, remaining)] == left[(stream, remaining)], "kept until two lease periods after expiry"
    assert lab.lines("b.jsonl") == [later[stolen[0]]]
    wait_for(lambda: owner(scan(table)[(stream, remaining)]) != dead, 10, "the expired shard taken over")
    wait_for(lambda: len(lab.lines("b.jsonl")) >= 2, 10, "the survivor's messages")
    time.sleep(1.5)
    assert survivor.stop() == 0
    assert as_multiset(lab.lines("a.jsonl")) == as_multiset(first)
    assert as_multiset(lab.lines("b.jsonl")) == as_multiset(later.values())
    assert scan(table) == {(stream, s): final_row(stream, s, later[s]["sequence"]) for s in shards}


def test_crash_before_any_checkpoint_strands_the_shard(lab):
    stream, shards = lab.stream(1)
    table = lab.table()
    sent = [lab.put(stream, f"r{i}", "pk") for i in range(2)]
    slow = {"commit_period": "6s", "lease_period": "2s"}
    lab.write_config("a.yaml", consumer_config([stream], table, out="a.jsonl", **slow))
    lab.write_config("b.yaml", consumer_config([stream], table, out="b.jsonl", **slow))
    crashed = lab.start("a.yaml")
    wait_for(lambda: len(lab.lines("a.jsonl")) >= 2, 30, "first instance messages")
    crashed.kill()  # before its first commit: the row holds only the claim
    claim = scan(table)[(stream, shards[0])]
    assert set(claim) == {"StreamID", "ShardID", "ClientID", "LeaseTimeout"}, claim
    lab.put(stream, "r2", "pk")
    restarted = lab.start("b.yaml")
    wait_for(lambda: owner(scan(table)[(stream, shards[0])]) != owner(claim), 20, "the expired claim to be taken")
    # The takeover waits (dead lease - lease_period + commit_period + steal_grace_period), then
    # resumes from the stored sequence; there is none, so no consumer ever starts.
    grace_end = parse_time(claim["LeaseTimeout"][1]) - timedelta(seconds=2) + timedelta(seconds=6 + 1)
    time.sleep(max(0.0, (grace_end - now()).total_seconds()) + 6)
    assert lab.lines("b.jsonl") == []
    assert restarted.running()
    rows = scan(table)
    assert set(rows) == {(stream, shards[0])}
    stranded = rows[(stream, shards[0])]
    assert set(stranded) == {"StreamID", "ShardID", "ClientID", "LeaseTimeout"}, stranded
    assert owner(stranded) != owner(claim)
    assert restarted.stop() == 0
    after = scan(table)[(stream, shards[0])]
    assert set(after) == {"StreamID", "ShardID", "ClientID", "LeaseTimeout"} and owner(after) == owner(stranded), after
    assert lab.lines("b.jsonl") == []
    assert as_multiset(lab.lines("a.jsonl")) == as_multiset(sent)


# ------------------------------------------------------------ resharding


def _split(stream, shard):
    KINESIS.split_shard(StreamName=stream, ShardToSplit=shard, NewStartingHashKey=str(2**126))
    wait_for(lambda: KINESIS.describe_stream_summary(StreamName=stream)["StreamDescriptionSummary"]["StreamStatus"] == "ACTIVE", 30, "ACTIVE after split")
    listed = KINESIS.list_shards(StreamName=stream)["Shards"]
    children = sorted(s["ShardId"] for s in listed if s.get("ParentShardId") == shard)
    assert len(children) == 2, listed
    return children


def test_children_of_a_split_shard_are_claimed_while_running(lab):
    stream, (parent,) = lab.stream(1)
    table = lab.table()
    before = [lab.put(stream, f"p{i}", f"pk{i}") for i in range(3)]
    lab.write_config("c.yaml", consumer_config([stream], table))
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 3, 30, "parent messages")
    rows = wait_for(lambda: (r := scan(table)) and r.get((stream, parent), {}).get("SequenceNumber") == ("S", before[2]["sequence"]) and r, 15, "parent checkpoint")
    client = owner(rows[(stream, parent)])
    children = _split(stream, parent)
    kids = [lab.put(stream, "low", "pk-low", 0, 4, children[0]), lab.put(stream, "high", "pk-high", 3, 4, children[1])]
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 5, 20, "child messages")
    expected = {(stream, k["shard"]): k["sequence"] for k in kids}
    rows = wait_for(lambda: (r := scan(table)) and all(r.get(key, {}).get("SequenceNumber", (0, 0))[1] == seq for key, seq in expected.items()) and r, 15, "child checkpoints")
    for key, seq in expected.items():
        assert assert_live(rows[key], *key, seq, 5) == client
    assert instance.stop() == 0
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset(before + kids)
    rows = scan(table)
    for key, seq in expected.items():
        assert rows[key] == final_row(*key, seq)


def test_fresh_start_skips_a_closed_parent(lab):
    stream, (parent,) = lab.stream(1)
    table = lab.table()
    lab.put(stream, "p0", "pk")
    children = _split(stream, parent)
    kids = [lab.put(stream, "low", "pk-low", 0, 4, children[0]), lab.put(stream, "high", "pk-high", 3, 4, children[1])]
    lab.write_config("c.yaml", consumer_config([stream], table))
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 2, 30, "child messages")
    time.sleep(3)
    assert set(scan(table)) == {(stream, c) for c in children}
    assert instance.stop() == 0
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset(kids)
    assert scan(table) == {(stream, k["shard"]): final_row(stream, k["shard"], k["sequence"]) for k in kids}


def test_closed_parent_can_be_consumed_explicitly(lab):
    stream, (parent,) = lab.stream(1)
    table = lab.table()
    before = [lab.put(stream, f"p{i}", "pk") for i in range(2)]
    children = _split(stream, parent)
    lab.put(stream, "low", "pk-low", 0, 4, children[0])
    lab.write_config("c.yaml", consumer_config([f"{stream}:{parent}"], table))
    instance = lab.start("c.yaml")
    wait_for(lambda: len(lab.lines("out.jsonl")) >= 2, 30, "parent messages")
    wait_for(lambda: (scan(table) or {}).get((stream, parent), {}).get("SequenceNumber") == ("S", before[1]["sequence"]), 15, "parent checkpoint")
    time.sleep(1.5)
    assert set(scan(table)) == {(stream, parent)}
    assert instance.stop() == 0
    assert as_multiset(lab.lines("out.jsonl")) == as_multiset(before)
