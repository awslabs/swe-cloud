import uuid

import pytest

from sweep import Account


@pytest.fixture
def bare_account():
    """A fresh, empty account without an IAM account alias."""
    account = Account()
    try:
        yield account
    finally:
        account.cleanup()


@pytest.fixture
def account(bare_account):
    """A fresh account that passes the safety gate with the default configuration."""
    bare_account.set_alias("sweep-" + uuid.uuid4().hex[:10])
    return bare_account
