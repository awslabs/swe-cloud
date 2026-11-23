# Form owners are still getting duplicate responses

**Service:** GC Forms reliability pipeline in `/app` — the worker that emails queued form
submissions through GC Notify, and the endpoint that receives Notify's delivery-status callbacks.

After the 2023-01-31 incident (SQS kept redelivering a message that had already been processed and
~200 responses went out twice), we shipped the `NotifyState` gate and callback reprocessing — see
`CHANGELOG.md`. Support is still getting duplicate reports:

- Owners receive the same response two or three times. Support's Notify search shows separate
  notifications for one submission, seconds apart.
- The reports cluster around worker instance replacements, the afternoons when Notify is slow,
  and days with many full-inbox bounces. One owner whose inbox was full for a few minutes got
  three copies of a single response once it cleared.

Fix it so that:

- A form owner gets one email per submission. If Notify reports `temporary-failure` or
  `technical-failure` for that email, they get exactly one replacement; `delivered` and
  `permanent-failure` are final.
- No response is ever lost: not when a worker dies at any point (including in the middle of a
  Notify call), not when a message comes back, not when callbacks repeat or arrive late. It still
  goes out within a minute.
- When a submission is done, its ReliabilityQueue item says so: `NotifyState` is `SENT` or
  `DELIVERED` (`FAILED` after a permanent failure), and `NotificationId` is the id of the email
  Notify accepted last for that submission (`docs/OPERATIONS.md`).
- Nothing is left behind on the work queues or the DLQ.

How it is deployed (`docs/OPERATIONS.md`), which the fix has to live with:

- Up to four worker instances and one callback server, each on its own machine. Any of them can
  be killed without warning and is replaced. They share nothing but the AWS services and Notify:
  no local files, sockets or memory between processes.
- Keep the entry points (`python3 -m reliability worker --worker-id <id>` and
  `python3 -m reliability callback --port <port>`), the environment variables listed in
  `README.md`, the callback path, and one-message-at-a-time polling with waits of at most 1 s.
- Queues, queue attributes and the table belong to infrastructure: do not create queues or change
  their attributes. The forms API writes the items (`tools/submit.py` shows exactly what it
  writes); do not rely on it writing anything else.
- GC Notify is not ours, and it is not idempotent (`docs/notify-api.md`).
- The fix ships like every deploy: the running workers and callback server are stopped without
  warning and yours start in their place. There is no maintenance window, so the table, the queues
  and Notify still hold whatever the current code left in them, including submissions it was in the
  middle of. Those have to come out right too.

The AWS endpoint is on `http://localhost:4566`. `README.md` shows how to run a local stack with a
fake Notify.
