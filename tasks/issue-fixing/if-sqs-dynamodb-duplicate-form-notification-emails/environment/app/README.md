# GC Forms — reliability pipeline

Form submissions are not emailed from the request path. The forms API stores each submission
in the `ReliabilityQueue` DynamoDB table and drops a pointer (`{"submissionID": ...}`) on the
submission queue. This service drains that queue and hands each response to GC Notify, which
emails it to the form owner. Notify then reports what happened to each email through a
delivery-status callback, and a submission whose email Notify could not deliver for a
transient reason is queued again on the reprocess queue.

```
forms API ──PutItem──▶ ReliabilityQueue (DynamoDB)
    │                        ▲   ▲
    └──SendMessage──▶ submission-queue ─┐   │   │
                                        ├─▶ worker(s) ──POST /v2/notifications/email──▶ GC Notify
                      reprocess-queue ──┘                                                │
                             ▲                                                           │
                             └────────── callback server ◀── POST /api/notify-callback ──┘
```

## Layout

| path | what |
|---|---|
| `reliability/worker.py` | the worker: port of the Reliability Lambda and its two event source mappings |
| `reliability/callback.py` | the delivery-status callback endpoint (port of `/api/notify-callback`) |
| `reliability/store.py` | ReliabilityQueue data layer |
| `reliability/notify.py` | GC Notify API client |
| `reliability/render.py` | builds the email from the stored form data |
| `tools/` | local stack: `bootstrap.py`, `submit.py`, `fake_notify.py` |
| `docs/OPERATIONS.md` | how this runs in production, the item contract, what can go wrong |
| `docs/notify-api.md` | the parts of the GC Notify API we depend on |

## Running it

```
python3 -m reliability worker --worker-id <id>     # one per worker instance
python3 -m reliability callback --port 8026        # one callback server
```

Both read their configuration from the environment (`reliability/config.py`):
`AWS_ENDPOINT_URL`, `AWS_DEFAULT_REGION`, `SUBMISSION_QUEUE_URL`, `REPROCESS_QUEUE_URL`,
`TABLE_NAME`, `NOTIFY_API_URL`, `NOTIFY_API_KEY`, `NOTIFY_TEMPLATE_ID`, `NOTIFY_CALLBACK_TOKEN`,
`CALLBACK_PORT`. The deployment sets all of them; the queue URLs and table name change between
environments, so never hard-code them.

Local stack (the AWS endpoint is already running on `http://localhost:4566`):

```
eval "$(python3 tools/bootstrap.py)"
python3 tools/fake_notify.py &                       # Notify on :8025, callbacks to :8026
python3 -m reliability callback --port 8026 &
python3 -m reliability worker --worker-id dev-1 &
python3 tools/submit.py --email owner@example.gc.ca
curl -s localhost:8025/_dev/notifications
```
