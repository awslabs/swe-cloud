"""A clean destination: everything accepted, everything delivered, in order."""

import harness


def test_clean_stream():
    failures, report = harness.run_phase("healthy")
    print("\n".join(report))
    assert not failures, "\n" + "\n".join(failures)
