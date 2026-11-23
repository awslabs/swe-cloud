# Spot interruption handling — operations

Owner: platform team. Applies to every consumer of the `spot-interruptions` queue.

## Topology

- One SQS standard queue, `spot-interruptions`, in the shared compute account. The org-wide
  EventBridge rule sends it every EC2 Spot Instance Interruption Warning in the account. An
  interruption event does not say which cluster or fleet an instance belongs to, so the rule
  cannot route per consumer. EC2 warns once per instance, so an interrupted instance has exactly
  one message on the queue, and the queue hands that message to one receiver at a time: nobody
  else receives it while its visibility timeout runs.
- Consumers of the queue:
  - `prod-a`: NTH (this repository), 3 replicas;
  - `prod-b`: NTH (this repository), 2 replicas;
  - `batch-c`: the batch fleet's own interruption handler (not NTH). It owns the spot
    instances that are in neither cluster and it plays by the same rules as everyone else on
    this queue.
- The queue's configuration (visibility timeout 30 s, retention, no redrive policy) belongs to
  the platform team and is not changed by consumers.
- NTH's IAM role allows `sqs:ReceiveMessage`, `sqs:ChangeMessageVisibility`,
  `sqs:DeleteMessage`, `sqs:GetQueueAttributes` and `sqs:GetQueueUrl` on this queue, and
  nothing else in AWS.

## NTH replicas

- Every replica is its own pod, scheduled on a different node. Replicas share nothing but the
  queue and the cluster API: no volume, no peer-to-peer traffic, no leader election. Pods run
  with a read-only root filesystem; `HOME`/`TMPDIR` (the working directory) is a per-pod
  emptyDir.
- NTH pods run on spot capacity themselves. A replica can disappear at any moment without
  running any shutdown code (its node is reclaimed, or it is OOM-killed); Kubernetes starts a
  replacement a few seconds later. This is why each cluster runs several replicas: whatever a
  dead replica was doing has to be picked up by the ones still running.
- A replica can also lose the cluster API and nothing else. We have had pods whose connection
  to the API server went dead (the API server instance behind it was replaced, or the path from
  that node broke): requests on it get no answer and no error until the client gives up, and it
  stays that way for minutes, while the pod keeps running and still reaches SQS normally. The
  other replicas of the cluster are not affected.
- Rolling restarts send SIGTERM; after `terminationGracePeriodSeconds` (5 s) the pod is killed.

## Cluster API

- The same API in every cluster (endpoints in `nth/kube.py`; `devcluster` serves it locally).
- Nodes run 10–18 pods. Every workload has a PodDisruptionBudget that allows one disruption at
  a time, and pods take about 4 s to terminate after an eviction (preStop hooks; the eviction
  call is answered once the pod is gone), so a drain takes 40–75 s and cannot be made faster.
- The API server sheds load: under pressure any call can be answered 429 or 503 with a
  `Retry-After` header. Callers retry.

## SLOs for an interruption notice

For a notice about a node of cluster X:

1. **Drain starts promptly.** X's NTH cordons the node within 10 s of the notice reaching the
   queue — or, if no replica of X was running at that moment, within 10 s of the first one
   starting.
2. **Drain finishes in time.** The node is fully drained within 100 s of the notice. The
   instance is reclaimed 120 s after the notice; the last 20 s belong to the kubelet.
3. **One replica per node.** At any moment at most one live replica cordons or evicts on a
   given node. A second replica working a node that another replica is still draining is an
   incident: eviction 429 storms, duplicate events, and drains that blow SLO 2.
4. **Other consumers' notices.** A notice about an instance that is not ours belongs to another
   consumer on the queue. It must reach its owner as quickly as it would if we were not on the
   queue at all (the other consumers have the same SLO 1), and it must still be there for them.
5. **Clean up.** Once the drain has succeeded, the notice is removed from the queue.
