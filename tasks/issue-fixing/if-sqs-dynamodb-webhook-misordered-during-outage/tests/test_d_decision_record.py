"""The decision record for the availability request."""

import harness


def test_decision_record():
    failures, report = harness.verify_decision()
    print("\n".join(report))
    assert not failures, "\n" + "\n".join(failures)
