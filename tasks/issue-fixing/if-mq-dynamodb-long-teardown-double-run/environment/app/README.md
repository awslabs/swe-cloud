# vm-destroyer

Tears down VMs the VM service no longer needs. The VM service publishes one destroy request per VM
to the `vm.destroy` queue; a pool of destroyer workers consumes them, runs the cloud team's
`vm-destroy` CLI for each VM, and records the outcome in the fleet table.

## How it runs

One worker process per destroyer host, started by the host's supervisor:

```
python3 -m destroyer.worker --worker-id <host-name>
```

with `PYTHONPATH` pointing at this repository and these environment variables:

| Variable | Meaning |
|---|---|
| `AMQP_URL` | the Amazon MQ (RabbitMQ) broker, credentials included |
| `WORK_QUEUE` | `vm.destroy` |
| `FLEET_TABLE` | the DynamoDB fleet table |
| `AWS_ENDPOINT_URL`, `AWS_DEFAULT_REGION`, credentials | the AWS account |

The supervisor does not restart a worker that exits; a host whose worker died is replaced by the
next deploy. Hosts share nothing but the broker and the AWS account: no disk, no memory, no
clocks beyond NTP. When a host is lost, its whole process tree goes with it.

## The broker

The broker is run by platform-messaging (Amazon MQ, RabbitMQ 3.13). They own `vm.destroy`; its
definition is `deploy/platform/vm.destroy.json` and it is applied by their tooling, not ours. We
must not delete, purge or redeclare it with other arguments, or attach policies to it. We may
declare our own queues and exchanges on the broker; prefix them `destroyer.`.

Destroy requests are persistent JSON messages on the default exchange, routed to `vm.destroy`:

```json
{"vm_id": "vm-0a1b2c3d"}
```

The VM service publishes each request once. It may publish a request again for a VM that has
already been destroyed (its retry is blind); that must be a no-op.

## The fleet table

One item per VM, keyed by `vm_id` (string). `state` moves `active` → `destroying` → `destroyed`.
The capacity scheduler releases a VM's quota when it reads `destroyed`, so `destroyed` is written
only after `vm-destroy` exited 0 for that VM. Other services read `state`, `destroyed_by` and
`destroyed_at`; they ignore any other attribute. See `destroyer/fleet.py`.

## vm-destroy

Installed on every host by the cloud team at `/usr/local/bin/vm-destroy`; not part of this repo
and not ours to change. `vm-destroy --help` documents it. In short: it walks the whole teardown of
one VM, prints `progress <n>` about every two seconds while the teardown advances, exits 0 when the
VM is gone, and is not idempotent. Workers run it as a child process (`destroyer/teardown.py`).

## Monitoring

From the incident review of the last consumer outage:

- **vm-destroy-no-consumers** pages when `vm.destroy` has had ready messages and zero consumers
  for 30 seconds.
- The destroyer dashboard compares the consumer count on `vm.destroy` with the number of running
  workers. A worker that is not busy is subscribed to `vm.destroy`; once the fleet is idle, every
  running worker is subscribed and the queue is empty.

## Local development

```
sandbox-cloud --vm vm-big=180 &           # cloud API for vm-destroy; per-VM teardown durations
eval "$(python3 tools/dev_stack.py)"      # dev broker with the platform's vm.destroy, fleet table
python3 -m destroyer.worker --worker-id dev1 &
python3 tools/request_destroy.py vm-small vm-big
```

`sandbox-cloud --help` lists its options; its stdout is the sandbox's teardown audit log.
