# Changelog

## 2023-01-31

- Worker: record `NotifyState=SENT` and the Notify `NotificationId` on the submission once Notify
  accepts the email, and acknowledge without sending when a message for an already-sent
  submission comes back. Follows the incident where SQS kept redelivering a message that had
  been processed and ~200 form responses were emailed again over two days (forms#1582).
- Queues: visibility timeout raised to 6 × the handler budget (see docs/OPERATIONS.md).
- Callback: on `temporary-failure` / `technical-failure`, put the submission back to `PENDING` and
  queue it on the reprocess queue so the response is sent again.

## 2022-11-08

- Reprocess queue added; the worker polls it alongside the submission queue.
