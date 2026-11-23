# Catalog entities left stale after bursts of edits

Ticket from the catalog platform on-call:

> For a few weeks now some catalog entities have been left stale: `processed_seq` stays behind
> `dirty_seq`, and the search document shows old data, until a later edit comes in — sometimes many
> minutes later. When we look, the refresh queue is empty, no job failed, and the worker logs look
> normal: the entity's last refresh ran and succeeded, it just ran before the last edit, and nothing
> ran after it. It mostly hits entities that are edited in bursts while a refresh of them is already
> running (bulk imports, merchandisers saving repeatedly).

The refresher lives in `/app` (`/app/README.md` describes how it works and what it guarantees,
`/app/CHANGELOG.md` its recent history). Fix it so that every edit is again covered by a refresh
that starts after it. Everything the README promises must still hold afterwards; in particular the
duplicate-job pileup that 2.4.0 fixed must not come back.

This fix ships to the worker fleet only. `dispatch` runs inside the catalog service, which vendors
the release that is in production now and will not be redeployed for several weeks: the messages it
queues and the lock items it writes stay exactly as they are today. The old workers are stopped and
the new ones started in one go, so the queue the new workers find can hold jobs at any delivery
count, including ones the old workers already ran or put back.

One blind spot at the switch is acceptable: for a job the old workers already handled, if the same
entity got another accepted dispatch within a second or two after that job's own, nothing the old release
wrote says which of the two the dedup lock belongs to. Either reading is fine there.

Constraints:

- Keep the worker's interface: `python3 -m refresher worker` with its flags and output, the
  configuration variables, and the way it runs `REFRESH_CMD` (arguments, environment, exit status).
  A retry or put-back stays a redelivery of the same SQS message.
- Several workers and many dispatchers run at the same time as separate processes on separate
  hosts. They share nothing but the queue and the tables; local files are not shared.
- The queue and the tables are the ones `refresher provision` creates; don't rely on any other AWS
  resource.

The local AWS endpoint is already running (`start_floci.sh` restarts it if needed). Work in `/app`.
