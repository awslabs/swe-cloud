"""A service stopped at a rejected delivery and its replacement."""

import harness


def test_replacement_after_stop():
    failures, report = harness.run_phase("crash")
    print("\n".join(report))
    assert not failures, "\n" + "\n".join(failures)
