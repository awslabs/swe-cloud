"""The repossessor: gives back the slots of leases nobody renewed.

A lease expires when the process holding it stopped renewing without reporting a state (killed,
node lost). Every REPOSSESS_LOOP_S the repossessor lists the expired members of the expirations
index and revokes each lease, returning its slots to the counters it was taken from.

Run with `python -m orchestrator.repossessor` (PG_DSN, VALKEY_URL, REPOSSESS_LOOP_S). It is safe
to run while the orchestrator serves, and it is restarted by the platform whenever it exits.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid

import psycopg

from . import config, db
from .release import consume_lease
from .leases import LeaseStorage
from .pause import pause_point

logger = logging.getLogger("orchestrator.repossessor")


def revoke_expired_lease(conn: psycopg.Connection, leases: LeaseStorage, lease_id: uuid.UUID) -> None:
    expired_lease = leases.read_lease(lease_id)
    if expired_lease is None:
        # The record is gone but the index may still list it; revoking clears the entry so the
        # lease stops being reported as expired on every pass.
        leases.revoke_lease(lease_id)
        logger.warning("Lease %s should be revoked but was not found", lease_id)
        return

    logger.info(
        "Revoking lease %s for %d concurrency limits with %d slots",
        lease_id, len(expired_lease.resource_ids), expired_lease.slots,
    )
    # Give the slot back (once, whoever else is releasing it), commit, and only then delete the
    # record: dying in between leaves a released lease behind, which the next pass cleans up.
    with conn.transaction():
        consume_lease(conn, lease_id, expired_lease, None, released_by="repossessor")
    pause_point("reap", lease_id=str(lease_id))
    leases.revoke_lease(lease_id)


def monitor_expired_leases(conn: psycopg.Connection, leases: LeaseStorage) -> int:
    expired = leases.read_expired_lease_ids()
    if expired:
        logger.info("Revoking %d expired leases", len(expired))
    for lease_id in expired:
        revoke_expired_lease(conn, leases, lease_id)
    return len(expired)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = config.load()
    if not settings.pg_dsn:
        raise SystemExit("PG_DSN is not set (see README: python -m orchestrator.devstack up)")
    conn = db.connect(settings.pg_dsn)
    db.ensure_schema(conn)
    leases = LeaseStorage(settings.valkey_url)
    leases.ping()
    print("READY", flush=True)
    while True:
        started = time.monotonic()
        try:
            monitor_expired_leases(conn, leases)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            logger.exception("Postgres connection lost; reconnecting")
            conn = db.connect(settings.pg_dsn)
        except Exception:  # noqa: BLE001
            logger.exception("repossessor pass failed")
        time.sleep(max(0.0, settings.repossess_loop_s - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
