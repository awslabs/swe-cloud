# Ledger writer: needless failovers, and a corrupted ledger after a real one

`fenced_worker` (in `/app`, see `/app/README.md`) elects one writer for the reconciliation ledger
through a DynamoDB lease lock and appends to `ledger.json` in S3 while it holds the lock. Two
incidents from last week, both on the pair `worker-1` (holder) / `worker-2` (standby):

**1. Healthy holder gave up its lock.** During a DynamoDB brownout `worker-1` logged a failed
heartbeat (one occurrence ended in a reset connection, another in an `InternalServerError`), then
`ConditionalCheckFailedException` on the next attempt, and stopped appending. About seven seconds
later `worker-2` took over and the ledger moved to fence 2. The lock-table stream shows that the
"failed" heartbeat was applied: the lock item still named `worker-1` and carried the version tag
from that very request until `worker-2` replaced it. Nobody else ever touched the item in between.
The lease is sized to ride out a brownout of a few seconds; a holder that still owns the lock must
not walk away from it.

**2. Ledger damaged after a legitimate failover.** In one occurrence `worker-1`'s host lost
DynamoDB and a ledger `PutObject` it had sent before that only reached S3 about nine seconds
later, after `worker-2` had taken over and appended. In another, `worker-1`'s host froze for
roughly ten seconds in the middle of an append. Afterwards the reconciliation job found an
acknowledged `worker-2` entry missing from the ledger, and `worker-1` entries with fence 1 after
`worker-2`'s fence-2 entries. The failover itself was correct; the writes afterwards were not.

Fix `fenced_worker` so that neither happens again, under both kinds of trouble at once: a holder
whose lock is still its own keeps it and keeps appending, and a holder that has been replaced
never gets a write into the ledger behind its successor. Failover must keep working and stay
inside the expectation in the README.

What must stay as it is:

- The command line, the `READY` line, the journal and its line format, the owner name format, and
  the lock item and ledger layouts described in the README. Other teams' clients share the lock
  table and the reconciliation job reads the ledger as it is.
- Every AWS call goes through `--endpoint`. The ledger stays in the one S3 object; the lock stays
  in the one DynamoDB item, renewed with `UpdateItem` heartbeats.
- Workers run as separate processes with separate working directories and share nothing local.
- Deploys restart the workers one at a time with SIGTERM, and the holder hands the lock over on its
  way out (README). Deploys keep happening, brownout or not; that handover has to keep working.

You can run Floci locally (`bash /usr/local/bin/start_floci.sh`, then `http://localhost:4566`).
