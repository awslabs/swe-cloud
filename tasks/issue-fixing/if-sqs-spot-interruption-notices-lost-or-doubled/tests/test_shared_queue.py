"""Shared spot-interruption queue: prod-a and prod-b NTH replicas plus batch-c's handler.

One recorded run, several independent checks against docs/OPERATIONS.md.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import rig as rigmod  # noqa: E402

DRAIN_OPS = ("cordon", "evict")
CONFIG_KEYS = ("VisibilityTimeout", "MessageRetentionPeriod", "DelaySeconds",
               "ReceiveMessageWaitTimeSeconds", "MaximumMessageSize", "RedrivePolicy",
               "FifoQueue", "ContentBasedDeduplication")


@pytest.fixture(scope="module")
def run():
    rig = rigmod.Rig()
    try:
        rig.run()
    except BaseException:
        try:
            rig.teardown()
        finally:
            rig.dump()
        raise
    data = rig.dump()
    return data


def _ops(run, label):
    return sorted((op for op in run["ops"] if op["node"] == label and op["op"] in DRAIN_OPS
                   and (run["teardown_at"] is None or op["t"] < run["teardown_at"])),
                  key=lambda op: op["t"])


def test_owning_cluster_starts_draining_promptly_after_another_cluster_saw_the_notice(run):
    """SLO 1: prod-a cordons within 10 s of its first replica starting (prod-b saw it first)."""
    start = run["t_prod_a_launch"]
    problems = []
    for label in ("a-n1", "a-n5"):
        cordons = [op["t"] for op in _ops(run, label)
                   if op["op"] == "cordon" and op["status"] == 200 and op["cluster"] == "prod-a"]
        if not cordons:
            problems.append(f"{label}: never cordoned")
        elif min(cordons) - start > rigmod.PICKUP_BOUND_S:
            problems.append(f"{label}: first cordon {min(cordons) - start:.1f}s after prod-a "
                            "started")
    assert not problems, "; ".join(problems)


def test_every_node_drained_before_the_deadline(run):
    """SLO 2: fully drained within 100 s of the notice, including after a replica died."""
    problems = []
    for label, node in run["nodes"].items():
        if node["published_at"] is None:
            problems.append(f"{label}: notice never published (run did not get that far)")
        elif node["drained_at"] is None:
            problems.append(f"{label}: not drained, {node['remaining']} of {node['pods']} pods "
                            "left")
        elif node["drained_at"] - node["published_at"] > rigmod.DEADLINE_S:
            problems.append(f"{label}: drained {node['drained_at'] - node['published_at']:.1f}s "
                            "after the notice")
    assert not problems, "; ".join(problems)


def test_one_live_replica_works_a_node(run):
    """SLO 3: a second replica only works a node once the first one is dead or cut off.

    A replica whose cluster-API connection went dead can no longer reach the node (none of its
    requests is executed after that), so a successor may take over from that moment on.
    """
    gone = {cid: min((t for t in (r["killed_at"], r["cut_at"]) if t is not None), default=None)
            for cid, r in run["replicas"].items()}
    problems = []
    for label in run["nodes"]:
        holder = None
        for op in _ops(run, label):
            if holder is None:
                holder = op["replica"]
            elif op["replica"] != holder:
                died = gone.get(holder)
                if died is not None and op["t"] >= died:
                    holder = op["replica"]
                else:
                    problems.append(
                        f"{label}: {op['replica']} {op['op']} ({op['status']}) at "
                        f"+{op['t'] - run['t0']:.1f}s while {holder} was still working it")
                    break
    assert not problems, "; ".join(problems)


def test_other_consumers_notice_reaches_its_owner(run):
    """SLO 4: batch-c's notice is still on the queue, unchanged, when batch-c comes up."""
    owner = run["foreign"]["owner"]
    received = owner["original_received_at"]
    assert received is not None, (
        f"batch-c never received its notice {run['foreign']['message_id']} "
        f"(copies seen: {owner['copies']})")
    window_end = run["t0"] + rigmod.OWNER_START_S + rigmod.OWNER_WINDOW_S
    assert received <= window_end


def test_queue_is_empty_once_everything_is_drained(run):
    """SLO 5: drained nodes' notices are removed; nothing is left behind on the shared queue."""
    counts = run["final_counts"]
    assert counts is not None, "could not read the queue's attributes"
    visible = int(counts.get("ApproximateNumberOfMessages") or 0)
    hidden = int(counts.get("ApproximateNumberOfMessagesNotVisible") or 0)
    delayed = int(counts.get("ApproximateNumberOfMessagesDelayed") or 0)
    assert (visible, hidden, delayed) == (0, 0, 0), counts


def test_shared_queue_and_account_untouched(run):
    """The queue's configuration is the platform team's; NTH's role is SQS-only on this queue."""
    before, after = run["attrs_before"], run["attrs_after"]
    assert after is not None, "could not read the queue's attributes"
    changed = {k: (before.get(k), after.get(k)) for k in CONFIG_KEYS if before.get(k) != after.get(k)}
    assert not changed, f"queue configuration changed: {changed}"
    created = {}
    for service, listed in (run["resources_after"] or {}).items():
        earlier = (run["resources_before"] or {}).get(service)
        if listed is None or earlier is None:
            continue
        new = sorted(set(listed) - set(earlier))
        if new:
            created[service] = new
    assert not created, f"resources created during the run: {created}"
