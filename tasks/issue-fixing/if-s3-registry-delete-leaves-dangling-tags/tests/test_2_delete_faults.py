"""A delete interrupted by a crash or by storage errors is finished by the janitor's retries."""

import pytest

from faultproxy import MUTATING
from harness import REVISION_MUTATION, TAG_MUTATION, check_end_state, wait_fired_or_done

N_TAGS = 40


def _killed_delete(cluster, key_re, nth, n_tags=N_TAGS):
    repo, (m, d), (mx, x), seeded = cluster.seed_case("crash", n_tags)
    cluster.strict_202 = True
    rule = cluster.rule("a", "hold", MUTATING, key_re, nth=nth)
    first = cluster.a.async_(cluster.a.delete, repo, d)
    if wait_fired_or_done(rule, first, 15):
        cluster.note(f"killing replica a at {rule.held[0].method} {rule.held[0].keys[:3]}")
        cluster.a.kill()
        cluster.proxy.drop_all(rule)
    else:
        cluster.note("hold rule did not fire before the delete finished")
    cluster.proxy.remove(rule)
    attempts = [first.result(60)]
    cluster.a.start()
    if attempts[0].status not in (202, 404):
        attempts += cluster.janitor_delete(cluster.a, repo, d)
    problems = check_end_state(cluster, repo, d, seeded, "other", x, attempts=attempts)
    return problems + cluster.problems_202


@pytest.mark.parametrize("nth", [1, 7])
def test_replica_killed_during_tag_cleanup(cluster, nth):
    problems = _killed_delete(cluster, TAG_MUTATION, nth)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("nth", [1, 2])
def test_replica_killed_at_revision_write(cluster, nth):
    problems = _killed_delete(cluster, REVISION_MUTATION, nth)
    assert not problems, "\n".join(problems)


def test_storage_errors_during_tag_cleanup(cluster):
    repo, (m, d), (mx, x), seeded = cluster.seed_case("s3errors", N_TAGS)
    cluster.strict_202 = True
    rule = cluster.rule("a", "fail500", MUTATING, TAG_MUTATION, nth=3, last=0)
    attempts = cluster.janitor_delete(cluster.a, repo, d,
                                      first=lambda r: cluster.proxy.remove(rule))
    cluster.proxy.remove(rule)
    cluster.note(f"500 rule matched {rule.count} requests")
    problems = check_end_state(cluster, repo, d, seeded, "other", x, attempts=attempts)
    problems += cluster.problems_202
    assert not problems, "\n".join(problems)
