"""The coupled scenario, first: a destination that rejects its oldest event."""

import harness


def test_ordered_during_rejection():
    failures, report = harness.run_phase("live")
    print("\n".join(report))
    assert not failures, "\n" + "\n".join(failures)
