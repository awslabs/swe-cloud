# Changelog

## 2.4.1 — 2026-10-12

- `provision` prints the table names it created.

## 2.4.0 — 2026-10-05

- Worker: retries no longer give up the dedup lock. A refresh releases the entity's lock when it
  starts, so a dispatch made while it runs is accepted and takes the lock. When that refresh failed,
  each retry released the lock again on its way in, deleting the lock the newer dispatch held. The
  next dispatch then queued a second copy, whose own failures did the same, and so on: during
  INC-2291 (search backend timeouts, 2026-09-28) one entity had 140 refresh jobs queued and the
  queue backed up for every entity. Retries never took a lock, so they have none to release; only
  the first delivery of a message releases it now.
- Overlap guard: `RELEASE_AFTER` default lowered from 30 s to 5 s, so a put-back job is picked up
  sooner after the running refresh finishes.

## 2.3.0 — 2026-08-17

- `dispatch --unique-for` to override the dedup lock lifetime per call (bulk imports use a short
  one).
- `dispatch` prints the SQS message id of an accepted job.

## 2.2.0 — 2026-06-30

- Moved from the Redis queue to SQS. Retries and put-backs change the message's visibility instead
  of re-pushing it; the delivery count comes from `ApproximateReceiveCount`.
- Dedup and overlap locks moved to DynamoDB with explicit `expires_at`.

## 2.1.0 — 2026-05-02

- Overlap guard, so two refreshes of one entity never run at once.
