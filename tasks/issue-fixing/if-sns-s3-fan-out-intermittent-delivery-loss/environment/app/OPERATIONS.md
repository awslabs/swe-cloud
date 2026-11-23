# Dispatch transport

The service sends each logical publication through SNS to standard SQS queues. The configured subscriber map is the fixed set of business projections that must process each publication; monitoring subscriptions are not business projections. Identities and the number of projections vary by deployment. Event IDs identify immutable publications within a deployment. Changes to membership are outside the current support envelope.

The body store holds one immutable object, `body_prefix + event_id`, containing the original bytes. SNS carries a JSON pointer with `event_id`, `body_key`, and the hex `sha256` of those bytes. The serialized pointer is at most 2048 bytes. No other body copies belong in S3, queue messages, coordination records, or worker caches. A projection that still needs an event must fetch that object and pass its bytes to the handler. Additional small coordination records in DynamoDB are permitted; `state_prefix` reserves their namespace.

Storage operations requires each event's object to disappear within 10 seconds after the last required projection has successfully returned from its handler. Bodies must remain available while a projection still needs them, including error-queue dwell and replay. There is no lifecycle executor in this environment. Workers are kept running during normal service, and restarted within a second after unexpected exit. Cleanup must resume across such restarts, including an interruption during finalization. A stopped projection can remain behind indefinitely while the others continue.

The standard queues may redeliver notifications, even with a new SQS message ID. Completion is per logical event and projection; an extra notification for work durably completed may be acknowledged without fetching or invoking the handler again. A handler interrupted before a durable completion may be invoked again (at-least-once). A crash must not turn incomplete work into completed work. Successful completion cannot be inferred merely from receiving a message.

Handler exceptions get up to `handler_attempts` attempts using queue visibility redelivery, then the original SQS body is sent unchanged to `error_queue_url` with String message attribute `failed_queue` containing the projection name. Body hydration failures go there immediately. Error transfer is a fresh SendMessage, not a native dead-letter redrive; its enqueue timestamp starts again. Operators may leave these copies there for minutes, then resend the body unchanged to the input queue named by `failed_queue` and delete the error copy. These copies are still required work. Permanently discarding error copies is outside this service's current contract.

The handler module is selected with `handler_module` (default `business`). Its function is `handle(subscriber: str, event_id: str, body: bytes) -> None`. It belongs to the application team; keep this call boundary and do not catch a failed invocation as success. Test and production hosts can install their own handler module on PYTHONPATH.

The upstream producer retries a `publish` command with the same event ID when its outcome is unknown, possibly after every projection has completed. The same ID and bytes must succeed without starting new logical work or recreating a reclaimed body. Reusing an ID with different bytes must fail before changing its stored body or delivery state, including after reclamation. Calls for one event ID do not overlap, and the file stays unchanged during a call; different IDs and subscribers run concurrently. Producer retries are not a recovery daemon: its local file and working directory can disappear after a crash. Once the body upload has returned successfully, running subscribers must recover the publication and deliver it within 15 seconds when services and handlers are healthy, even if the producer never retries. A crash before a successful upload may require a producer retry. SNS acceptance and the caller's knowledge of it are separate events; duplicate notifications following an uncertain send are normal. The storage SLO still applies while a producer is paused or its result is unknown. A reclaimed body must stay absent on subsequent retries.

Use `python /app/fanout.py --help`. Set `FANOUT_CONFIG` to an absolute JSON file. `setup` idempotently prepares any transport-owned durable state; it must not replace the topic, queues, subscriptions, or bucket. `publish --event-id ID --body-file PATH` returns success only after SNS has accepted the publication (or durable evidence shows an earlier attempt was accepted). `subscriber --name NAME` runs until SIGTERM and prints `READY NAME` when it begins polling. Each process has its own working directory and can die without preserving its disk or memory. The `seam` calls in the processing flow are process instrumentation used by the host; retain their names and facts at the equivalent flow positions. Publisher positions are `after_upload` (announced as soon as the body PUT returns, before any other service call: the original bytes are durably in S3 and SNS has not been called) and `after_notify` (SNS returned success, before recording its outcome), both with `subscriber="publisher"` and the event ID. A retry that needs neither a PUT nor an SNS call need not announce these positions.

All services use `http://localhost:4566`, region `us-east-1`, credentials `test` / `test`. Start with `bash /usr/local/bin/start_floci.sh`. Provision the resources with AWS CLI or boto3, then use a config like:

```json
{
  "endpoint_url": "http://localhost:4566",
  "region": "us-east-1",
  "topic_arn": "arn:aws:sns:us-east-1:000000000000:events",
  "bucket": "dispatch-bodies",
  "body_prefix": "bodies/",
  "subscribers": {"billing": "http://localhost:4566/000000000000/billing", "search": "http://localhost:4566/000000000000/search"},
  "error_queue_url": "http://localhost:4566/000000000000/errors",
  "state_prefix": "dispatch_",
  "handler_attempts": 3,
  "handler_module": "business"
}
```

For a provisioned deployment:

```
export FANOUT_CONFIG=/absolute/path/config.json
python /app/fanout.py setup
python /app/fanout.py subscriber --name billing
python /app/fanout.py publish --event-id order-731 --body-file /absolute/path/event.bin
```
