# Manifest service

Generates file manifests for the data portal. Generating a manifest means querying the catalog index
for every file the filters select and writing the result to the manifest bucket. That can take
minutes, so it happens asynchronously in a Step Functions execution, and the client polls a token.

```
client ──PUT──▶ service instance (×N) ──StartExecution──▶ state machine manifest-gen
   ▲                 │   ▲                                   │ Generate (waitForTaskToken)
   │   301 + token   │   │ HeadObject                        ▼
   └─────────────────┘   │                         SQS manifest-gen-tasks ──▶ generator
                         └──────────── S3 manifest bucket ◀── writes <object_key>, then
                                                              SendTaskSuccess / SendTaskFailure
```

- `manifest_service/` — the service (this team). Several instances run behind the load balancer;
  each is a separate process and any of them may receive any request, including the polls for a
  token another instance handed out. Instances are restarted on every deploy and whenever they crash.
- `generator/` — the generator (data team). Not ours to change.
- `deploy/` — the state machine definition (data team) and a bootstrap script for a local deployment.

## API

All responses are JSON. Redirects are not sent as HTTP redirects. They are described in the body of
an HTTP 200 response, so the portal can follow them on its own schedule.

### `PUT /v1/manifests?catalog=<catalog>&format=<format>&filters=<json>`

Request a manifest. `format` is `compact` or `verbatim.jsonl`. `filters` is a JSON object mapping a
field to exactly one operator and its values, e.g. `{"organ": {"is": ["brain"]}}`. Field order is not
significant: the portal and the command-line scripts serialise the same selection differently, and
both get the same manifest.

- The manifest is in the bucket: `{"Status": 302, "Location": "s3://<bucket>/<object_key>"}`.
- Otherwise: `{"Status": 301, "Location": "/v1/manifests/<token>", "Retry-After": <seconds>,
  "Execution": "<execution ARN>"}`. The client waits `Retry-After` seconds and follows `Location`.
  `Execution` is the generation attempt the token tracks; support uses it to look the attempt up.

### `GET /v1/manifests/<token>`

Poll a generation attempt.

- Still running: another `301` of the same shape, with a new token and a longer `Retry-After`.
- Succeeded and the manifest is in the bucket: the `302` above.
- Succeeded but the manifest is no longer in the bucket (it expired): the service generates it
  again and answers `301` for that.
- The attempt failed (or was aborted, or timed out): HTTP 500,
  `{"Code": "ServerError", "Message": "('Manifest generation failed', <status>, <error>)"}`.
  Polling a failed attempt always reports that failure; the portal shows it and offers to try again,
  which sends a fresh `PUT`.

Errors: HTTP 400 `BadRequestError` for invalid parameters or tokens, HTTP 500 otherwise.

### `GET /health`

`{"up": true}` once the instance accepts requests.

## Generation attempts

Every generation attempt is one execution of the Standard state machine `manifest-gen`. The service
names the execution after the manifest (`<generation id>_<iteration>`, see `async_service.py`), so
every instance that receives a request for the same manifest computes the same name. Step Functions
treats a `StartExecution` with the name *and input* of a running execution as a no-op that returns
that execution; once an execution has ended, its name stays taken (on AWS, for 90 days after it
closes) and `StartExecution` answers `ExecutionAlreadyExists`.

### Contract with the generator

The execution input is the JSON object built in `ManifestController._start_execution`. The generator
reads `catalog`, `format`, `filters` and `object_key` from it. On success it first writes the manifest
to `s3://$MANIFEST_BUCKET/<object_key>` and then completes the task with the output
`{"object_key": "<object_key>", "rows": <n>}`. On failure it fails the task, which fails the execution
(e.g. error `IndexUnavailable` when the catalog index times out). The generator never retries.

## Configuration

A deployment provides each service instance with exactly this, and with no other AWS resources:

| variable | |
|---|---|
| `MANIFEST_STATE_MACHINE_ARN` | the `manifest-gen` state machine |
| `MANIFEST_BUCKET` | the manifest bucket |
| `MANIFEST_CATALOGS` | optional, comma-separated, default `dcp,lungmap` |
| `AWS_ENDPOINT_URL`, `AWS_DEFAULT_REGION`, credentials | standard boto3 variables |

The generator additionally gets `MANIFEST_TASK_QUEUE_URL` and `CATALOG_INDEX_TABLE`.

In production the bucket has a lifecycle rule that expires manifests after
`config.manifest_expiration` (7) days.

## Quotas

Step Functions throttles its API per account and region, with one token bucket per action: a call
takes a token, and the bucket refills at a fixed rate up to its size. The production account runs
with the default quotas for us-east-1:

| action | bucket size | refill per second |
|---|---|---|
| `StartExecution` | 1,300 | 300 |
| `DescribeExecution` | 300 | 15 |
| `ListExecutions` | 200 | 5 |
| `GetExecutionHistory` | 400 | 20 |
| `DescribeStateMachine` | 200 | 20 |
| `ListStateMachines` | 100 | 5 |

The buckets are shared by everything in the account: every service instance, every request and
every token poll. A call that finds its bucket empty fails with `ThrottlingException`. boto3 retries
it as configured in `manifest_service/aws.py`, and if that doesn't get through, the client gets a 500.
The emulator does not throttle. S3 request rates are far above anything the service does.

## Running locally

The emulator answers on `http://localhost:4566`.

```sh
cd /app
eval "$(python3 deploy/bootstrap.py)"            # state machine, queue, bucket, index; prints exports
python3 -m generator.worker &                    # the generator
python3 -m manifest_service --port 8081 &        # one service instance; start more on other ports
curl -s -X PUT 'localhost:8081/v1/manifests?catalog=dcp&format=compact' \
     --data-urlencode 'filters={"organ": {"is": ["brain"]}}' -G
curl -s localhost:8081/v1/manifests/<token>
```

`deploy/bootstrap.py` is idempotent; it also restores the catalog index if it was deleted.

## Operations

- A generation that hangs is aborted with `StopExecution`; the next request for that manifest must
  then produce it.
- Every failed generation trips the 5xx alarm through the 500 its pollers receive.
