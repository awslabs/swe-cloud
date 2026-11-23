# fulfilment-relay

Drains the `orders` SQS standard queue and calls the fulfilment service once per order. It is a
single long-running process, `/app/run-consumer`, with no state of its own: everything that has to
survive lives in SQS.

## Configuration (environment)

| Variable | Meaning |
|---|---|
| `SQS_ENDPOINT_URL` | The SQS endpoint. In production this is our SQS gateway; it is the only SQS endpoint the relay talks to. |
| `QUEUE_URL` | URL of the orders queue. |
| `WORK_URL` | Fulfilment service endpoint. |
| `CONCURRENCY` | Handler slots, i.e. fulfilment calls that may be open at once (default 4). |
| `REDELIVER_VISIBILITY_TIMEOUT_SECONDS` | How long a failed order stays hidden before SQS hands it out again (default 0: immediately). |
| `RECEIVE_WAIT_TIME_SECONDS` | Long-poll wait per ReceiveMessage (default 1). |
| `WORK_TIMEOUT_SECONDS` | HTTP timeout of a fulfilment call (default 120). |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | As usual. |

The queue itself (visibility timeout, redrive policy, dead-letter queue) is owned by infra. The
relay reads the queue's `VisibilityTimeout` at startup and never changes queue attributes; it does
not send messages.

## What the relay does with an order

1. **Receive.** The receive loop long-polls whenever a handler slot is free and hands each
   delivery to a slot.
2. **Lease.** Every delivery gets a visibility lease: a heartbeat that calls
   `ChangeMessageVisibility` on that delivery's receipt handle every 0.7 x VisibilityTimeout, so an
   order stays hidden however long fulfilment takes.
3. **One at a time per order.** SQS standard queues are at-least-once: the same message (same
   `MessageId`) can be delivered again while an earlier delivery is still being worked on, for
   example when a heartbeat did not get through in time. The relay tracks in-flight deliveries by
   `MessageId` and never has two fulfilment calls open for the same order.
4. **Fulfil.** `POST WORK_URL` with JSON `{"message_id": <SQS MessageId>, "receive_count":
   <ApproximateReceiveCount>, "body": <message body>}`. A 2xx answer means the order was
   fulfilled; anything else (or no answer within the timeout) is a fault. This payload is the
   fulfilment API's contract.
5. **Settle.**
   - Success: the message is deleted.
   - Fault: the message's visibility is set to `REDELIVER_VISIBILITY_TIMEOUT_SECONDS`, so SQS hands
     it out again after that delay and it is retried.
   - Each receive counts toward the queue's redrive policy. An order that keeps failing is moved to
     the dead-letter queue by SQS once it has been received `maxReceiveCount` times; that is the
     only way an order gets there.

## SQS behaviour we rely on

See `docs/sqs-notes.md` for the AWS wording. In short:

- Every receive of a message comes with a new receipt handle. Only the handle from the most recent
  receive is guaranteed to act. AWS documents this for `DeleteMessage` ("If you use an old
  ReceiptHandle, the request will succeed, but the message might not be deleted"). Our gateway
  behaves the same way for `ChangeMessageVisibility`. A call made with an older handle returns
  success and has no effect.
- A visibility change is not remembered across deliveries: a new delivery starts from the queue's
  own visibility timeout and has to be kept hidden by whoever holds its handle.
- `ChangeMessageVisibility` to 0 makes the message receivable immediately.

## Running it locally

An SQS emulator (Floci) listens on `http://localhost:4566` in this environment.

```bash
python3 /app/devtools/make_queues.py dev-orders          # prints the QUEUE_URL; VT 6 s, redrive 5 -> dev-orders-dlq
python3 /app/devtools/stub_fulfilment.py 8081 &           # a fulfilment stand-in that accepts every order
SQS_ENDPOINT_URL=http://localhost:4566 QUEUE_URL=<printed url> WORK_URL=http://127.0.0.1:8081/work \
    /app/run-consumer
aws --endpoint-url http://localhost:4566 sqs send-message --queue-url <url> --message-body '{"order_id":"o-1"}'
```

Logs go to stderr.
