"""Multi-tenant workflows: the same frontend user in two tenant pools never crosses into the other tenant."""

import threading
import time

import psycopg
import pytest

from conftest import run_pooler, tenant_pools, wait_until
from pgwire import refusal

pytestmark = pytest.mark.usefixtures("tenants")


def identity(conn):
    return conn.execute(
        "SELECT current_database()::text, current_user::text, session_user::text, "
        "(SELECT string_agg(tenant, ',' ORDER BY tenant) FROM marker)").fetchall()


def test_each_alias_reaches_its_own_database_and_backend_role(tenants, pooler):
    with pooler.alpha() as a, pooler.beta() as b:
        assert identity(a) == [(tenants.alpha.database, tenants.alpha.owner, tenants.alpha.owner, "alpha")]
        assert identity(b) == [(tenants.beta.database, tenants.beta.owner, tenants.beta.owner, "beta")]
        # Interleaving the two sessions does not swap them.
        assert identity(a)[0][0] == tenants.alpha.database
        assert identity(b)[0][0] == tenants.beta.database


@pytest.mark.parametrize(("alias", "password"), [("beta", "alpha-secret"), ("alpha", "beta-secret")])
def test_password_of_one_tenant_is_refused_by_the_other(tenants, pooler, alias, password):
    before = tenants.activity()
    codes, errors, kinds = refusal(pooler.port, alias, "app", password)
    assert (codes, errors) == ([5], [("FATAL", "28P01")])
    assert b"Z" not in kinds
    with pytest.raises(psycopg.OperationalError):
        pooler.connect(alias, "app", password)
    # Neither tenant gained a server session from the refusals; both still work with their own password.
    assert tenants.activity() == before
    with pooler.alpha() as a, pooler.beta() as b:
        assert a.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("alpha",)]
        assert b.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("beta",)]


def test_interleaved_transactions_on_same_names_stay_in_their_tenant(tenants, pooler):
    with pooler.alpha() as a, pooler.beta() as b:
        a.execute("BEGIN")
        b.execute("BEGIN")
        a.execute("INSERT INTO ledger VALUES ('k1', 1)")
        b.execute("INSERT INTO ledger VALUES ('k1', 2)")
        # Each sees only its own uncommitted row; nothing is visible outside yet.
        assert a.execute("SELECT k, v FROM ledger").fetchall() == [("k1", 1)]
        assert b.execute("SELECT k, v FROM ledger").fetchall() == [("k1", 2)]
        assert tenants.ledger(tenants.alpha) == []
        assert tenants.ledger(tenants.beta) == []
        activity = tenants.activity()
        assert sorted(activity) == [(tenants.alpha.owner, tenants.alpha.database, "idle in transaction"),
                                    (tenants.beta.owner, tenants.beta.database, "idle in transaction")]
        a.execute("INSERT INTO ledger VALUES ('k2', 10)")
        a.execute("COMMIT")
        assert tenants.ledger(tenants.alpha) == [("k1", 1), ("k2", 10)]
        assert tenants.ledger(tenants.beta) == []
        assert b.execute("SELECT k, v FROM ledger ORDER BY k").fetchall() == [("k1", 2)]
        b.execute("ROLLBACK")
        assert tenants.ledger(tenants.beta) == []
        assert tenants.ledger(tenants.alpha) == [("k1", 1), ("k2", 10)]
        # After the transactions each session still reads only its own tenant.
        assert a.execute("SELECT k, v FROM ledger ORDER BY k").fetchall() == [("k1", 1), ("k2", 10)]
        assert b.execute("SELECT count(*) FROM ledger").fetchall() == [(0,)]


def test_exhausted_tenant_does_not_block_the_other(tenants, pooler):
    holders = [pooler.alpha() for _ in range(2)]
    try:
        for index, conn in enumerate(holders):
            conn.execute("BEGIN")
            conn.execute(f"INSERT INTO ledger VALUES ('hold{index}', {index})")
        assert wait_until(lambda: tenants.activity(tenants.alpha.owner) == [
            (tenants.alpha.owner, tenants.alpha.database, "idle in transaction")] * 2)
        with pooler.beta() as b:
            started = time.monotonic()
            assert b.execute("SELECT (SELECT tenant FROM marker), current_user::text").fetchall() == [
                ("beta", tenants.beta.owner)]
            b.execute("BEGIN")
            b.execute("INSERT INTO ledger VALUES ('b', 1)")
            b.execute("COMMIT")
            assert time.monotonic() - started < 0.9  # below alpha's checkout timeout
        assert tenants.ledger(tenants.beta) == [("b", 1)]
        activity = tenants.activity()
        assert activity.count((tenants.alpha.owner, tenants.alpha.database, "idle in transaction")) == 2
        assert all((user, db) in {(tenants.alpha.owner, tenants.alpha.database), (tenants.beta.owner, tenants.beta.database)}
                   for user, db, _ in activity)
        assert sum(1 for user, _, _ in activity if user == tenants.beta.owner) >= 1
        assert tenants.ledger(tenants.alpha) == []
    finally:
        for conn in holders:
            conn.close()


def test_backend_pools_do_not_cross_under_concurrent_load(tenants):
    with run_pooler(pools=tenant_pools(tenants, alpha_size=2, beta_size=2),
                    general={"connect_timeout": 10000}) as pooler:
        errors = []
        seen = {"alpha": set(), "beta": set()}

        def worker(alias, password, n):
            try:
                with pooler.connect(alias, "app", password) as conn:
                    for i in range(6):
                        conn.execute("BEGIN")
                        row = conn.execute("SELECT current_database()::text, current_user::text, "
                                           "(SELECT tenant FROM marker)").fetchone()
                        conn.execute(f"INSERT INTO ledger VALUES ('{alias}-{n}-{i}', {i})")
                        conn.execute("COMMIT")
                        seen[alias].add(row)
            except Exception as exc:  # noqa: BLE001 -- report every worker failure
                errors.append((alias, n, repr(exc)))

        threads = [threading.Thread(target=worker, args=(alias, pw, n))
                   for n in range(4) for alias, pw in (("alpha", "alpha-secret"), ("beta", "beta-secret"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(50)
        assert errors == []
        assert seen == {"alpha": {(tenants.alpha.database, tenants.alpha.owner, "alpha")},
                        "beta": {(tenants.beta.database, tenants.beta.owner, "beta")}}
        assert tenants.ledger(tenants.alpha) == sorted((f"alpha-{n}-{i}", i) for n in range(4) for i in range(6))
        assert tenants.ledger(tenants.beta) == sorted((f"beta-{n}-{i}", i) for n in range(4) for i in range(6))
        activity = tenants.activity()
        assert 1 <= sum(1 for row in activity if row[0] == tenants.alpha.owner) <= 2
        assert 1 <= sum(1 for row in activity if row[0] == tenants.beta.owner) <= 2
        assert all(row[1] == (tenants.alpha.database if row[0] == tenants.alpha.owner else tenants.beta.database)
                   for row in activity)
