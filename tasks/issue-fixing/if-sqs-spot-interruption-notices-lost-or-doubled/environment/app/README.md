# nth — node termination handler, queue-processor mode

A Python port of [aws-node-termination-handler](https://github.com/aws/aws-node-termination-handler)
(v1.25.x, queue-processor mode) that the platform team runs in our EKS clusters. It long-polls the
spot-interruption queue, and for every EC2 Spot Instance Interruption Warning about one of the
cluster's nodes it cordons the node, evicts its pods, and then removes the notice from the queue.
The structure follows upstream so that upstream fixes can be carried over:

| module | upstream |
|---|---|
| `nth/__main__.py` | `cmd/node-termination-handler.go` (monitor ticker, interruption loop, workers) |
| `nth/sqs_monitor.py` | `pkg/monitor/sqsevent/sqs-monitor.go` |
| `nth/spot_itn.py` | `pkg/monitor/sqsevent/spot-itn-event.go` |
| `nth/drain.py` | `pkg/interruptionevent/draincordon/handler.go` |
| `nth/event_store.py` | `pkg/interruptioneventstore` |
| `nth/kube.py` | the Kubernetes client calls (replaced by our cluster API, see the docstring) |
| `nth/config.py` | `pkg/config` |

Where the port differs from upstream: the managed-node check asks the cluster API whether it
knows the instance instead of reading EC2 tags; only spot interruption warnings are acted on
(rebalance, state-change and ASG lifecycle events are dropped); no webhooks, taints or metrics.

## How a message is handled

Each replica receives up to 10 messages at a time and decides per message:

- **our node** – the event goes to the event store; a worker cordons and drains the node, and
  the message is deleted once the drain succeeded;
- **not actionable** (an instance this cluster does not manage, a test notification, an event
  kind we do not handle) – the message is deleted;
- **error** – the message is left alone so that SQS delivers it again later.

## Configuration

Environment variables, as the Helm chart sets them:

| variable | meaning |
|---|---|
| `QUEUE_URL` | the interruption queue (required) |
| `CLUSTER_API_URL` | cluster API base URL (required) |
| `CLUSTER_API_TOKEN` | the pod's service-account token (required) |
| `POD_NAME` | this replica's pod name (log prefix, user agent) |
| `AWS_ENDPOINT_URL`, `AWS_DEFAULT_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | AWS access |
| `SQS_MSG_VISIBILITY_TIMEOUT_SEC` | optional, default 20, valid 1–119 |
| `WORKERS` | optional, concurrent drains per replica, default 10 |
| `NODE_TERMINATION_GRACE_PERIOD` | optional, seconds, default 120 (also the drain timeout) |
| `CHECK_TAG_BEFORE_DRAINING`, `DELETE_SQS_MSG_IF_NODE_NOT_FOUND`, `LOG_LEVEL` | optional |

Production sets only the required variables and `POD_NAME`; everything else is the default in
`nth/config.py`.

## Running it locally

The emulator answers on `http://localhost:4566` with the `AWS_*` variables already set in this
environment.

```bash
# a queue to play with
Q=$(awslocal sqs create-queue --queue-name nth-dev --query QueueUrl --output text)

# a cluster with two nodes (10 and 14 pods); tokens map to pod names in its access log
python3 -m devcluster --port 8701 \
    --node ip-10-0-1-17.ec2.internal=i-0f1e2d3c4b5a69780:10 \
    --node ip-10-0-2-40.ec2.internal=i-0a9b8c7d6e5f40312:14 \
    --token tok-1=nth-1 --token tok-2=nth-2 &

# one replica (start more with other POD_NAME/CLUSTER_API_TOKEN pairs)
QUEUE_URL=$Q CLUSTER_API_URL=http://127.0.0.1:8701 CLUSTER_API_TOKEN=tok-1 POD_NAME=nth-1 \
    python3 -m nth &

# an interruption notice
python3 -m devcluster.notice --queue-url "$Q" --instance-id i-0f1e2d3c4b5a69780
```

Operational requirements and the production topology are in `docs/OPERATIONS.md`.
