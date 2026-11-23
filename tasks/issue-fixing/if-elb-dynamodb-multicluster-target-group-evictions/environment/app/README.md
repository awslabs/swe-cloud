# tgb-controller

Keeps each cluster's Service endpoints registered in an ELBv2 target group. One controller process
runs per cluster. It reads the cluster's binding file, written by the cluster's endpoint agent, and
keeps the binding's endpoints registered as IP targets in an ELBv2 target group.

```
python3 /app/controller.py \
  --cluster-id cluster-a \
  --target-group-arn arn:aws:elasticloadbalancing:us-east-1:000000000000:targetgroup/web/... \
  --binding-file /run/tgb/cluster-a/binding.json \
  --aws-endpoint http://127.0.0.1:<cluster egress port> \
  --ledger-table tgb_ownership \
  --availability-zone all \
  --scratch-dir /var/run/tgb-controller/<fresh dir>
```

Layout: `controller.py` (CLI and loop), `tgb/binding.py` (binding file), `tgb/targets.py` (ELBv2
targets API and listing cache), `tgb/reconcile.py` (the reconcile pass), `tgb/aws.py` (clients).

## Binding file

```json
{"generation": 12, "deleted": false, "endpoints": ["10.20.0.4:8080", "10.20.0.9:8080"]}
```

The endpoint agent replaces the file atomically (write + rename) whenever the Service's ready pods
change. `endpoints` is the complete set of pod `ip:port` the cluster wants in the target group right
now. `generation` goes up with every change to the Service's ready pods, so of two snapshots the
one with the lower generation is the older. `deleted: true` means the binding was removed. The
controller must then take its cluster's targets out of the group and its cluster's items out of the
ownership table, and exit with status 0.

The endpoint agent runs as an active/standby pair. When the active agent's node fails, the standby
takes over. It first writes the last snapshot it had received, which can be older than the file it
replaces, and writes the Service's current state once it has caught up, up to fifteen seconds
later.

## The shared target group

The `web` target group is no longer owned by a single cluster. It is registered to by:

- cluster-a's controller and cluster-b's controller, one process each (blue/green and failover
  between the two clusters);
- the network team, who register a few legacy hosts by hand and add or remove them whenever
  they like.

Both clusters and the legacy hosts draw addresses from the same shared subnet, on the same port.
The legacy hosts sit on fixed addresses that are never handed to pods. Pod addresses are not
reserved per cluster: when a pod goes away its address goes back to the subnet, and once the
subnet's 30-second hold-down on released addresses has passed, the next pod to start in either
cluster may get it.
ELBv2 records nothing about who registered a target: a target is just `Id`, `Port` and
`AvailabilityZone`. Egress policy fixes the target description for everyone: every
RegisterTargets member must be exactly `{Id: <ip>, Port: 8080, AvailabilityZone: <the
--availability-zone value>}`, and deregistrations may carry only those fields with those values.
The egress proxy rejects anything else with a 400 and does not forward it.

## Ownership table

`tgb_ownership` (DynamoDB, on-demand) was provisioned for the shared-group rollout. It is keyed
`owner` (HASH, string, the cluster id) and `target` (RANGE, string, `"ip:port"`), one item per
target. You may add other attributes. Both clusters' items live in this one table. The table is
the cluster's record of which targets in the shared group are its own. Every target a cluster has
in the group must have an item under that cluster's id at all times. The cluster decommission
runbook depends on this. When a cluster is torn down without a working controller, the runbook
removes from the group exactly the targets listed under its id. A cluster's items must only ever
name that cluster's own endpoints, never the other cluster's pods or the legacy hosts.

## Operating conditions

- The supervisor runs exactly one controller per cluster. Controllers are killed without warning
  (node pressure, evictions, SIGKILL) at any point in their work. The supervisor restarts the
  controller with the same arguments and a new, empty `--scratch-dir`. Nothing on local disk or in
  memory survives a restart. Do not assume the other cluster's controller is running.
- All AWS traffic goes through the cluster's egress endpoint (`--aws-endpoint`). Use it for every
  AWS call. The egress path drops connections now and then. The egress proxy forwards a request
  as soon as it has received it and never queues, delays or replays one, so a request reaches AWS
  within moments of being sent or not at all, also when its controller dies meanwhile.
- The endpoint agent keeps updating the binding file while the controller is down.
- SLO: once a controller is running, the target group reflects its binding (its endpoints present,
  its removed pods gone) within 15 seconds.

## Local development

Floci (an AWS emulator) answers on `http://localhost:4566` (credentials `test`/`test`,
`us-east-1`). It has no egress proxy, so point `--aws-endpoint` straight at it. For example:

```
aws --endpoint-url http://localhost:4566 elbv2 create-target-group --name dev-web \
    --protocol TCP --port 8080 --target-type ip --vpc-id vpc-0dev
aws --endpoint-url http://localhost:4566 dynamodb create-table --table-name tgb_ownership \
    --attribute-definitions AttributeName=owner,AttributeType=S AttributeName=target,AttributeType=S \
    --key-schema AttributeName=owner,KeyType=HASH AttributeName=target,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST
```
