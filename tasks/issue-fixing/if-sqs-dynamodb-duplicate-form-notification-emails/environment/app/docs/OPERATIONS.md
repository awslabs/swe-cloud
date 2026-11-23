# Operations

## Deployment

| thing | value |
|---|---|
| work queues | `submission-queue` (forms API), `reprocess-queue` (callback server, support re-queues) — SQS standard |
| visibility timeout | 12 s on both, = 6 × the 2 s handler budget (AWS guidance for SQS consumers) |
| redrive | both work queues → `submission-dlq` at `maxReceiveCount` 5 |
| worker instances | up to 4, each its own instance; `--worker-id` is unique per incarnation |
| callback server | one instance; Notify posts to `http://<host>:$CALLBACK_PORT/api/notify-callback` |
| table | `ReliabilityQueue`, partition key `SubmissionID` (S), on-demand; every submission since launch |
| volume | a few thousand submissions on a weekday, more around filing deadlines |
| GC Notify | one live API key, shared by every worker and the callback server |

Queues, their attributes and the table are provisioned by infrastructure code, not by this
service. Do not create queues or change queue attributes from the application.

Worker instances are separate machines in production: the only things two workers (or a worker
and the callback server) share are the AWS services and Notify. Nothing on local disk survives an
instance, and an instance's disk is not visible to any other.

## The ReliabilityQueue item

Written by the forms API before the submission is enqueued (`tools/submit.py` writes the same
shape). The forms API is deployed separately; its item format is fixed.

| attribute | type | written by | meaning |
|---|---|---|---|
| `SubmissionID` | S | forms API | key; also what the queue message carries |
| `FormID`, `FormSubmissionLanguage`, `SecurityAttribute`, `CreatedAt` | S/S/S/N | forms API | metadata |
| `FormData` | S | forms API | the response as JSON, including `deliveryOption.emailAddress` |
| `SendReceipt` | S | forms API | SQS message id of the original enqueue |
| `NotifyState` | S | forms API (`PENDING`), this service | where the submission is in delivery |
| `NotificationId` | S | this service | GC Notify id of the email that stands for this submission |

Support and the monthly delivery reconciliation read only `NotifyState` and `NotificationId`.
When a submission is done, `NotifyState` is `SENT` (or `DELIVERED` once Notify has confirmed
delivery; `FAILED` for a permanent failure) and `NotificationId` is the id of the email the owner
actually received — the one Notify accepted for the latest send. Other states and attributes are
internal to this service. Items are never deleted by this service and have no TTL.

## Service level

Every submission must reach its owner, and within a minute of being queued under normal load —
including when the worker that picked it up dies. A form owner receives one email per submission.
When Notify reports a `temporary-failure` or `technical-failure` for that email, the owner
receives one replacement email; `delivered` and `permanent-failure` are final.

## What happens in production

- **Workers die.** Instances are replaced without warning: deploys, host maintenance, OOM. A
  worker can disappear at any instruction, including in the middle of a Notify call. A
  replacement starts about a second later with a new `--worker-id`. The 2 s handler budget is
  the expected processing time, not a kill.
- **SQS delivers at least once.** A message can be received again after its visibility timeout,
  an extra copy can be delivered even after a successful delete (the 2023-01-31 incident), and
  copies of one submission can be in flight on both work queues at the same time, in different
  workers.
- **Notify is sometimes slow.** p50 is ~100 ms, but a few times a day a send takes longer than the
  visibility timeout to answer; the email has been accepted long before the answer comes
  (docs/notify-api.md). Notify is not idempotent: every accepted POST is an email.
- **Callbacks repeat.** See docs/notify-api.md: one notification can produce several callbacks,
  failed callbacks are retried, and a callback can arrive late.
- **Deploys replace everything at once.** A release stops every worker and the callback server
  (the same as any instance replacement: no draining) and starts the new version against the same
  table, queues and Notify service. Items, queue messages, sent emails and undelivered callbacks
  from the previous version are all still there, and the new version takes them over.
- **Support re-queues.** Support can put a `{"submissionID": ...}` message on the reprocess queue
  when an owner says a response never arrived.

## Logs

One JSON object per line on stderr. `submissionId` is on every line about a submission.
