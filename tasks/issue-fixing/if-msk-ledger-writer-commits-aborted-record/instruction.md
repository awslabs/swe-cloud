# LEDGER-2291: duplicated ledger entry after the 21 Oct network incident

Reconciliation flagged settlement batch `STL-20261021-0417`: the committed ledger holds its entry
`STL-20261021-0417-2` twice, one copy stamped `"attempt": 1`, and the GL export for the day was
rejected. During the network incident that evening the active writer hit a delivery timeout on that
batch, aborted the attempt and retried the batch, and the retry committed. What we have is in
`/app/incident/`.

Fix the ledger writer in `/app` so that nothing from an aborted attempt can ever reach the committed
ledger, whenever the stalled request turns up. Riding out a timeout has to keep working the way it
does now: abort, retry the batch, carry on in the same process, and be committing again within the
SLO. Restarting the writer, or making it wait out the broker, is not a fix. The active/standby
arrangement described in `/app/OPERATIONS.md` has to keep working exactly as it is described there.

Keep the command line, the batch-file and record formats, the exit statuses and the producer settings
listed in `OPERATIONS.md`. Every writer instance is its own process in its own working directory;
nothing on local disk is shared between instances. `/app/README.md` shows how to bring up a
development MSK cluster and run the writer against it.
