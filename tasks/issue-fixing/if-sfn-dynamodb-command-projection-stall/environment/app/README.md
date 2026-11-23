# Command projection service

Each accepted `(entity, version)` has a DynamoDB row and an independently named
Standard execution. The publisher represents our Streams consumer: redelivery
can start distinct executions with the same input. Version 1 starts in sync;
later versions park in `wait_prev_command`, then sync, then finish. Queues are
standard SQS queues. Neither queue deduplication nor execution names give us
exactly-once business effects.

The projection sink is intentionally not an idempotency layer. It represents
external actions such as posting a balance delta. Each version must enter sync
once, after its predecessor's sync has committed. We treat the predecessor's
committed `finish:STARTED` as that boundary. The last finish status write may
still be outstanding when a later version starts. Status `sync_data:FINISHED`
is not a release boundary: finish admission can still be refused. A predecessor
that fails before finish leaves successors parked for operational recovery.

## Components and storage

`worker.py` consumes wait and finish messages; `manage.py` provisions and
publishes; `sink.py` applies sync effects. Each task message contains `entity`,
`version`, `taskToken`, `executionArn`. Waiting callbacks return JSON
`{"result":"resumed","by":"push"}` or the same with `"pull"`. A duplicate
execution may remain parked or complete with `{"result":"duplicate"}`, which
routes to a Succeed state without sync. Finish returns `{"result":"ok"}`.

`commands`: key `pk` (S: entity), `sk` (N: version), `status` (S), `taskToken` (S),
`executionArn` (S). Status is `created` or `<step>:STARTED|FINISHED|FAILED`.
`taskToken` records the callback associated with this version. Do not reset
another execution's status or erase tokens when acknowledging queue messages.
`effects`: `pk` (S: entity), `sk` (S: version#executionArn), plus version,
executionArn and predStatusAtSync. `projection`: `pk` (S), version (N).
Only the sink writes effects/projection. Only the publisher starts executions.
Rows are retained; this is not a historical chain repair tool.

## Deployment

Boot AWS with `bash /usr/local/bin/start_floci.sh`. From `/app`:

```
python3 manage.py init
./solution wait-worker
./solution finish-worker
python3 sink.py
python3 manage.py publish customer-17 1
python3 manage.py publish customer-17 2
```

Run consumers in separate terminals; production uses two wait consumers and
three finish consumers. `manage.py init` prints resource URLs. Defaults use
`commands`, `wait-prev`, `finish`, `sync`, and `command-chain`. Configuration:
`AWS_ENDPOINT_URL`, `AWS_REGION` (default us-east-1), `COMMANDS_TABLE`,
`WAIT_QUEUE_URL`, `FINISH_QUEUE_URL`. Credentials are test/test locally.
The sink and deployment tools also accept `SYNC_QUEUE_URL`.

The deployment service manages the queues and executions; workers receive and
delete messages and complete callbacks. They cannot send messages or alter queue
visibility, create/reconfigure resources or stop/start executions. Each worker
handles one message at a time. The wait handler has a three-second processing
budget from receipt to deletion; it persists its information and returns, not
an execution-long resident poller. There is no scanner or scheduled recovery
process. SQS visibility is 30 seconds and workflow waits time out after 120
seconds. A ready callback should release within eight seconds, including when a
status write on another worker takes 15 seconds. Read capacity allows 20 command
row reads per version across the workers, including retries.

DynamoDB's default reads may lag writes by three seconds. Independent executions
have no session consistency. Short transient read throttles occur; the service
uses bounded retries (three attempts, 100ms initial backoff). Persistent read
failure is logged for operators; a finish callback must not become FAILED merely
because it could not inspect the next row. SendTaskSuccess on a closed callback
returns TaskDoesNotExist or TaskTimedOut. Workers must remain available after
such races. Actual workflows and the sink run on Floci :4566; a service gateway
may be supplied as AWS_ENDPOINT_URL to restore these AWS semantics locally.
