"""Channel choice: a request reaches only enabled channels of the caller's group, by priority tier.

Selection inside a tier may be random, so these tests check which channels are reached over a run of
requests (membership), never a particular sequence.
"""

from collections import Counter

from checks import relay_error

RUN = 30


def _burst(relay, stubs, tag, group, count=RUN):
    user = relay.user(group)
    token = relay.create_token(user, name="route")
    statuses = [relay.chat(token["key"], f"{tag} #{i}")[0] for i in range(count)]
    reached = Counter(call["upstream"] for call in stubs.tagged(tag))
    return user, statuses, reached


def test_each_group_reaches_only_its_channel(relay, stubs, tag):
    for group in ("vip", "svip", "default"):
        _, statuses, reached = _burst(relay, stubs, f"{tag}-{group}", group, count=5)
        assert statuses == [200] * 5
        assert reached == Counter({group: 5}), (group, reached)
        auths = {call["auth"] for call in stubs.tagged(f"{tag}-{group}")}
        assert auths == {f"Bearer sk-upstream-{group}"}


def test_highest_priority_tier_wins(relay, stubs, tag):
    # tier: hi (priority 10), lo (priority 0), off (priority 20 but disabled)
    _, statuses, reached = _burst(relay, stubs, tag, "tier")
    assert statuses == [200] * RUN
    assert reached == Counter({"hi": RUN}), reached


def test_equal_priority_channels_share_requests(relay, stubs, tag):
    _, statuses, reached = _burst(relay, stubs, tag, "pair")
    assert statuses == [200] * RUN
    assert set(reached) == {"p1", "p2"} and sum(reached.values()) == RUN, reached


def test_non_positive_top_priority_uses_every_tier(relay, stubs, tag):
    # negtier: n0 (priority 0) and n5 (priority -5); the top-tier restriction needs a positive top priority.
    _, statuses, reached = _burst(relay, stubs, tag, "negtier")
    assert statuses == [200] * RUN
    assert set(reached) == {"n0", "n5"} and sum(reached.values()) == RUN, reached


def test_weight_does_not_exclude_a_channel(relay, stubs, tag):
    # flat: z1 (weight 100) and z2 (weight 0), both priority 0
    _, statuses, reached = _burst(relay, stubs, tag, "flat")
    assert statuses == [200] * RUN
    assert set(reached) == {"z1", "z2"} and sum(reached.values()) == RUN, reached


def test_channel_listed_for_several_groups(relay, stubs, tag):
    for group in ("alpha", "beta"):
        _, statuses, reached = _burst(relay, stubs, f"{tag}-{group}", group, count=3)
        assert statuses == [200] * 3
        assert reached == Counter({"shared": 3}), (group, reached)


def test_group_without_channel_is_unavailable(relay, stubs, tag):
    user = relay.user("nochan")
    token = relay.create_token(user, name="nowhere")
    for _ in range(3):
        status, reply = relay.chat(token["key"], tag)
        relay_error(status, reply, 503)
    assert stubs.tagged(tag) == []
    assert relay.user_row(user["id"]) == {"quota": 1_000_000, "used_quota": 0, "request_count": 0, "status": 1}
    assert relay.token_row(token["id"])["used_quota"] == 0


def test_serving_channel_recorded_in_usage_log(relay, stubs, tag):
    user, statuses, reached = _burst(relay, stubs, tag, "pair", count=12)
    assert statuses == [200] * 12
    relay.wait_for(lambda: relay.user_row(user["id"])["request_count"] == 12)
    entries = relay.logs(user, 0) + relay.logs(user, 1)
    assert len(entries) == 12
    logged = Counter(entry["channel"] for entry in entries)
    ids = relay.channel_ids
    assert logged == Counter({ids["p1"]: reached.get("p1", 0), ids["p2"]: reached.get("p2", 0)}) - Counter(), (logged, reached)
