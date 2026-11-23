# ghr control plane: scale-up

Our self-hosted CI runners are managed by a Python port of the control-plane lambdas
of [terraform-aws-github-runner](https://github.com/github-aws-runners/terraform-aws-github-runner)
(ported from the TypeScript at the release that contains PR #5220). This repository holds the
**scale-up** function and a local stack to run it.

```
CI webhook -> webhook fn -> SQS ghr-queued-builds -> (event source mapping) -> scale-up fn
                                      |  redrive after 3 receives                     | fleet API (EC2)
                                      v                                               | code host  (JIT config)
                                 ghr-queued-builds-dlq                                 | SSM /ghr/runners/<instance-id>
```

Runners are ephemeral, org-level and JIT-configured. For each queued `workflow_job` the scale-up
function launches one instance for that job, asks the code host for a JIT runner config named
`<prefix><instance-id>`, and writes it to SSM at `${SSM_TOKEN_PATH}/<instance-id>`. The instance
boots, reads its parameter, registers and runs the job. An ephemeral runner runs exactly one job:
when the job finishes, the instance is terminated, and from then on `describe-runners` no longer
lists it.

An instance with no parameter never registers. It idles until scale-down reaps it after the boot
timeout (15 minutes), and for all that time it is a running runner instance that counts towards
`RUNNERS_MAXIMUM_COUNT` like any other.

## Layout

| path | what |
|---|---|
| `control_plane/lambda_handler.py` | `scale_up_handler(event, context)`: SQS event in, `SQSBatchResponse` out |
| `control_plane/scale_up.py` | groups the batch by owner, cap check, launch, retry decisions |
| `control_plane/ec2.py` | launch + configure + clean up for one group |
| `control_plane/fleet.py` | fleet API client (EC2), error classification |
| `control_plane/github_runner.py`, `github.py` | JIT config from the code host |
| `control_plane/ssm.py` | Parameter Store |
| `control_plane/runtime.py` | local Lambda runtime adapter (one invocation per process) |
| `deploy/scale-up.json` | the deployed function, event source mapping and queue settings |
| `devstack/` | local stack: fleet API, code-host API, event source mapping poller |

## How the function is run

In production it is a Lambda (`deploy/scale-up.json`): 8 s timeout, reserved concurrency 1, fed by
an SQS event source mapping on `ghr-queued-builds` with batch size 10, a 2 s batching window and
`ReportBatchItemFailures`. The queue's visibility timeout is 15 s; after 3 receives a message is
moved to `ghr-queued-builds-dlq`.

Locally (and in our integration environment) the same function is run one invocation per process:

```bash
PYTHONPATH=/app python3 -m control_plane.runtime control_plane.lambda_handler.scale_up_handler < event.json
```

The event is the Lambda SQS event (`Records[]` with `messageId`, `receiptHandle`, `body`,
`attributes.ApproximateReceiveCount`, `eventSource: "aws:sqs"`, ...). stdout carries the response,
`{"batchItemFailures": [{"itemIdentifier": "<messageId>"}]}`, and nothing else; logs go to stderr.
`context.get_remaining_time_in_millis()` works; the invoker kills the process at the deadline.

Treat each invocation like a Lambda cold start: it runs in a fresh working directory and nothing on
local disk is carried to the next invocation. Anything that has to outlive one invocation lives in
Parameter Store under `SSM_TOKEN_PATH` or is read back from the fleet API.

The function's execution role is `iam_role` in `deploy/scale-up.json`, and it grants nothing else:
read/write/delete under `SSM_TOKEN_PATH`, and on the build queue only what the event source mapping
needs (receive, delete, get attributes). The function does not send messages; the only way a record
goes back on the queue is by being listed in `batchItemFailures`. (Upstream's separate job-retry
queue is not deployed.)

Endpoints come from the environment, as on Lambda: `AWS_ENDPOINT_URL` for AWS services,
`AWS_ENDPOINT_URL_SSM` and, where set, `AWS_ENDPOINT_URL_SQS` for SSM and SQS (the VPC interface
endpoints; the SDK picks them up by itself), `FLEET_API_URL`, `GITHUB_API_URL`. The function's own
settings are the `environment` block of `deploy/scale-up.json`.

### What the event source mapping does with the response

This is Lambda's documented behaviour for SQS with `ReportBatchItemFailures`, and what
`devstack/esm.py` reproduces:

- every record **not** listed in `batchItemFailures` is deleted from the queue;
- every listed record stays on the queue and is delivered again once its visibility timeout ends
  (its `ApproximateReceiveCount` goes up by one; past 3 it goes to the DLQ);
- an empty or null list is a complete success: the whole batch is deleted;
- an exception, a timeout, invalid JSON, or an `itemIdentifier` that is empty or not a messageId of
  the batch is a complete failure: nothing is deleted and the whole batch is delivered again.

Three receives, 15 s apart, will not outlast a long AWS outage, and they are not meant to: the DLQ is
the backstop for a job that could not be served in three deliveries, and on-call redrives it once the
outage is over.

### Events

Message body (written by the webhook function):

```json
{"id": 4711, "eventType": "workflow_job", "repositoryName": "app", "repositoryOwner": "org-a",
 "installationId": 1, "repoOwnerType": "Organization", "labels": ["self-hosted", "linux", "x64"]}
```

`id` is the workflow job id. Org-level runners only serve organisation repositories: an event whose
`repoOwnerType` is not `Organization` is ignored on purpose and acknowledged, so it is never
retried.

## Fleet API

JSON over HTTP at `FLEET_API_URL`; the platform team's front for EC2 (CreateFleet,
DescribeInstances, TerminateInstances, with our launch template and `ghr:*` tags). Errors are EC2's,
passed through as `{"__type": "<EC2 error code>", "message": "..."}` with EC2's HTTP status (for
example `503 Unavailable`, `503 RequestLimitExceeded`).

| call | body | answer |
|---|---|---|
| `POST /describe-runners` | `{"environment", "owner"}` | `{"instances": [{"instance_id", "owner", "job_id", "state", "launch_time"}]}`: the owner's running and pending runner instances |
| `POST /create-fleet` | `{"environment", "owner", "job_ids": [...]}` | `{"instances": [{"instance_id", "job_id"}], "errors": [{"error_code", "error_message"}]}` |
| `POST /terminate` | `{"instance_id"}` | `{}`; terminating an instance that is already terminated is a no-op |

`create-fleet` launches one instance per requested job and tags it with that job id
(`ghr:job_id`), which `describe-runners` returns for as long as the instance lives.

`describe-runners` is DescribeInstances underneath, and EC2's API is eventually consistent: an
instance that `create-fleet` has just returned can take up to 8 seconds to show up in
`describe-runners`. A terminated instance drops out at once. Like CreateFleet
it can fulfil part of a request: the instances it did launch are listed, and `errors` says why the
rest were not (for example `InsufficientInstanceCapacity`). Error codes in `SCALE_ERRORS` are
capacity problems worth retrying.

## Code-host API

`POST /orgs/{org}/actions/runners/generate-jitconfig` with `{"name", "runner_group_id", "labels"}`
answers `201 {"runner": {"id", "name", ...}, "encoded_jit_config"}`. Each call registers a new
runner; the config is what the instance needs in its SSM parameter.

## Local stack

```bash
devstack/up.sh                         # dev queues on Floci, fleet API :8081, code host :8082, ESM poller
python3 devstack/send_jobs.py org-a:3 org-b:2
tail -f /tmp/devstack/esm.log          # invocations, function logs, what was deleted / returned
curl -s localhost:8081/instances       # what the fleet launched
python3 -c 'import boto3; print(boto3.client("ssm").get_parameters_by_path(Path="/ghr/runners", WithDecryption=True)["Parameters"])'
devstack/down.sh
```

The dev fleet and code-host APIs never fail; production ones do. The dev fleet never runs jobs, so its
instances stay up until something terminates them.
