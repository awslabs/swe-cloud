import uuid
from dataclasses import dataclass

import pytest
from harness import workspace
from support import account, cleanup


@dataclass
class Names:
    """Unique names for one test; every AWS resource a test creates starts with `p`."""

    p: str
    tracking: str
    acct: str

    def sqs(self, name="q"):
        return f"arn:aws:sqs:us-east-1:{self.acct}:{self.p}-{name}"

    def sns(self, name="t"):
        return f"arn:aws:sns:us-east-1:{self.acct}:{self.p}-{name}"

    def role(self, name="r"):
        return f"arn:aws:iam::{self.acct}:role/{self.p}-{name}"


@pytest.fixture(scope="session")
def acct():
    return account()


@pytest.fixture
def ns(acct):
    token = uuid.uuid4().hex[:8]
    names = Names(p=f"x{token}", tracking=f"trk-{token}", acct=acct)
    try:
        yield names
    finally:
        cleanup(names.p)


@pytest.fixture
def ws():
    with workspace() as root:
        yield root
