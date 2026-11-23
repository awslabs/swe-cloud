"""Replicas share nothing but the bucket they are given."""

from conftest import BASELINE, aws_resources
import harness


def test_replicas_only_use_their_bucket(bucket):
    problems = []
    if harness.VIOLATIONS:
        sample = [f"{v['method']} {v['path'][:120]}" for v in harness.VIOLATIONS[:5]]
        problems.append(f"{len(harness.VIOLATIONS)} requests from replicas were not S3 calls on "
                        f"their bucket: {sample}")
    now = aws_resources()
    for kind in ("dynamodb", "sqs", "ssm", "s3"):
        new = now.get(kind, set()) - BASELINE.get(kind, set()) - {bucket}
        if new:
            problems.append(f"new {kind} resources appeared while replicas ran: {sorted(new)[:5]}")
    assert not problems, "\n".join(problems)
