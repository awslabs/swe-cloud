# relay

`relay` takes render jobs off an SQS standard queue and hands them to the render farm's intake
(the *downstream*), one at a time, deleting each message once the downstream has accepted it. Many
relay instances consume the same queue.

## Running it

```bash
cd /app   # or anywhere, with PYTHONPATH=/app
QUEUE_URL=... DOWNSTREAM_URL=http://127.0.0.1:8700/ python3 -m relay
```

| variable | default | meaning |
|---|---|---|
| `QUEUE_URL` | (required) | the queue to consume |
| `DOWNSTREAM_URL` | (required) | where each message is POSTed |
| `RELAY_ID` | `relay-<pid>` | the instance name, sent with every downstream call |
| `AWS_ENDPOINT_URL_SQS` | `AWS_ENDPOINT_URL` | the SQS endpoint; deployments point it at the SQS sidecar, always use it |
| `VISIBILITY_TIMEOUT` | `20` | seconds; set by the platform team, see below |
| `MAX_MESSAGES_PER_POLL` | `20` | messages per poll |
| `WAIT_TIME_SECONDS` | `10` | long-poll wait of each receive request |
| `CONCURRENT_REQUEST_LIMIT` | `50` | parallel receive requests per poll, at most |
| `EXTEND_MESSAGE_VISIBILITY` | `true` | keep held messages invisible while they wait and while they are handled |
| `POLL_DELAY_MS` | `500` | pause after a poll that returned nothing |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every receive, extension and delete |

`python3 -m relay --help` lists the same. In this sandbox SQS is the local emulator on
`http://localhost:4566` (credentials `test`/`test`, `us-east-1`); `python3 /app/tools/sink.py`
is a stand-in downstream that accepts every call after a fixed hold.

## How it works

- **Poll** (`relay/polling.py`). SQS returns at most 10 messages per `ReceiveMessage`, so a poll
  sends `ceil(MAX_MESSAGES_PER_POLL / 10)` receive requests in parallel, each for 10 messages with
  `VisibilityTimeout=VISIBILITY_TIMEOUT` and `WaitTimeSeconds=WAIT_TIME_SECONDS`. With the
  production setting of 20 that is two parallel long-poll receives per poll; this is the throughput
  configuration the capacity plan is based on and it stays.
- **Batch** (`relay/consumer.py`). The messages of the poll are one batch. Each is POSTed to the
  downstream in turn; a 2xx means the job is accepted and the relay deletes the message. Any other
  answer, or no answer, leaves the message in the queue to be received again.
- **Visibility** (`relay/extender.py`, `relay/scheduler.py`). While the relay holds a message,
  from the moment SQS hands it out until the downstream call for it has returned — including while
  it waits in the batch behind slower ones — no other instance may receive it. A single extender
  thread runs every `VISIBILITY_TIMEOUT / 2` seconds and sends `ChangeMessageVisibilityBatch` for
  the messages whose window is about to run out.

## Downstream

`POST DOWNSTREAM_URL` with a JSON body
`{"relay": RELAY_ID, "message_id": ..., "receipt_handle": ..., "body": <message body>}`. The call
blocks until the farm has taken the job, which can take from a fraction of a second to minutes.
The intake takes **one call at a time per relay instance**; a second concurrent call from the same
`relay` gets `429` and is not processed.

## Operating constraints

- **Visibility timeout.** `VISIBILITY_TIMEOUT` is owned by the platform team and is 20 s
  everywhere. Every receive and every extension uses exactly that value (handing a message back
  with `VisibilityTimeout=0` is fine). Short is the point: whatever a dead instance held must be
  back in the queue quickly. Changing the queue's attributes is not an option either (see the grant).
- **Crashes.** Instances get OOM-killed (SIGKILL) now and then. Everything a killed instance held
  must become receivable by the others within one visibility timeout of the last visibility change
  it made for it (its receive, or its last extension).
- **Deploys and scale-in (SIGTERM).** The relay finishes the message it is handing downstream —
  that one stays hidden until its call returns, and is deleted — and routes nothing else. Every
  other message it has received is let go at once: from the SIGTERM on it is no longer extended,
  so it is back in the queue at the latest one visibility timeout after the SIGTERM (handing it back
  early with `VisibilityTimeout=0` is fine). The process exits once that message is done and any receive
  in progress has returned.
- **Extension budget.** `ChangeMessageVisibility` calls share the account's SQS request quota with
  every other service; an older relay that re-extended every held message every second got the
  account throttled. The rule since then: a message is never extended within the first quarter of a
  visibility window — not within 5 s (at 20 s) of SQS handing it out, nor within 5 s of its previous
  extension. A message the downstream takes within a few seconds costs no extension call at all.
- **Grant.** The relay's role allows `ReceiveMessage`, `ChangeMessageVisibility(Batch)`,
  `DeleteMessage(Batch)`, `GetQueueAttributes` and `GetQueueUrl` on its queue, nothing else: it
  cannot send messages, create queues or change queue attributes.
- The queue is a standard queue without a dead-letter queue. Duplicate deliveries are possible in
  SQS regardless; what the relay controls is never letting a message go while it still holds it.

## Local run

```bash
aws --endpoint-url http://localhost:4566 sqs create-queue --queue-name jobs \
    --attributes VisibilityTimeout=20
python3 /app/tools/sink.py --port 8700 --hold 2 &
QUEUE_URL=http://localhost:4566/000000000000/jobs DOWNSTREAM_URL=http://127.0.0.1:8700/ \
    LOG_LEVEL=DEBUG python3 -m relay
```

(Use the `QueueUrl` that `create-queue` prints.)
