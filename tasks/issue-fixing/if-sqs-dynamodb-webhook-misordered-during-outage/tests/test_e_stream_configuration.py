"""The streams this verifier created, re-read after everything ran."""

import harness


def test_stream_configuration():
    failures, report = harness.verify_registry()
    print("\n".join(report))
    assert not failures, "\n" + "\n".join(failures)
