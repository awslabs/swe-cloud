# LEDGER-3418: ledger events missing downstream after month-end; relay must not skip or stall

Billing's month-end close found ledger events that are in the `outbox` table but were never
delivered on the `ledger-events` queue. Most of them are `ReconciliationStarted` events from the
reconciliation job, whose transactions stay open for a minute or more this month because the
bank's statement API was slow. There are also scattered missing events from the busiest hours of
the day. Consumers treat a skipped position as "never happened", so every one of these is a wrong
balance downstream.

Some history. The relay used to freeze: in INC-2207 it sat behind a rolled-back write for 41
minutes until someone moved its checkpoint by hand, and `stale_gap_seconds` came out of that
incident. Nobody wants the freeze back either.

The relay lives in `/app` (start with `/app/README.md`). Fix it so that:

- it never skips an event that is still going to commit, however long the writing transaction
  stays open;
- when a write is rolled back, or a duplicate insert is dropped, delivery of everything after it
  resumes within 20 seconds. This also has to hold when the relay is SIGKILLed and restarted in
  the meantime, which the platform does without warning, possibly several times in a row. The
  one thing it may wait longer for is a writer whose transaction was already open when the relay
  came across the gap (or came across it again after a restart): from where the relay sits, any of
  those could still be the one that fills it, so holding the gap until they have ended is right,
  even if one of them is the reconciliation job;
- everything else in the README's contracts keeps holding: position order on the one FIFO group,
  the message format, the checkpoint meaning and its lag going back to zero when producers are
  quiet, the 5 second delivery latency, and nothing at or below the starting checkpoint is sent
  again.

Constraints: the schema, the grants and the producer library (`db/`, `outbox/`) are owned by other
teams and are deployed as they are, and so are the producers' habits described in the README.
The relay keeps its command (`python3 /app/relay.py`), its environment variables, the `READY` line
and SIGTERM handling, and it runs as `outbox_relay`. `/app/ops/devstack.py` brings up a dev stack
on the local AWS endpoint. The fixed relay is what gets deployed from `/app`.
