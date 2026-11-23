# PLAT-4127: spot interruptions mishandled since prod-b joined the shared queue

Last week prod-b started consuming the shared `spot-interruptions` queue next to prod-a and the
batch fleet's handler (topology in `/app/docs/OPERATIONS.md`). Since then:

- **prod-a nodes reclaimed with pods still running.** Two prod-a spot nodes went away mid-shift
  with their workloads still on them. prod-a's NTH never logged the notices. prod-b's NTH logs
  have `dropping interruption event for unmanaged node` for both instance ids, a few seconds
  after the notices were sent.
- **Batch team:** their interruption handler has missed most notices for batch instances since
  the same day.
- **Double drains.** On the bigger nodes, the API server audit log shows a second NTH replica
  cordoning and evicting a node while another replica is still draining it, followed by a
  cascade of PDB 429s. It happens on prod-a and prod-b, mostly on nodes that take longer than
  20 s to drain.

Per-cluster queues are not an option: the platform team owns the queue and the EventBridge
rule, and the rule cannot tell clusters apart. NTH has to behave on a shared queue.

**Ask:** fix the handler in `/app/nth` so that prod-a, prod-b and batch-c can share the queue
and the SLOs in `docs/OPERATIONS.md` hold for every notice, with replicas coming, going and
failing the way that document describes.

What must stay as it is:

- The entry point `python3 -m nth` and the configuration contract in `/app/README.md`.
  Replicas get only the required variables, `POD_NAME` and the `AWS_*` ones; every tunable
  takes the default in the code.
- The cluster API. The rig serves the same API `devcluster` serves (same auth, eviction timing
  and disruption budgets).
- The queue: no changes to its configuration, and no other AWS resources (NTH's role is
  SQS-only on this queue).

How it will be checked: several replicas of each cluster run against a fresh shared queue and
the cluster API, next to batch-c's handler, with interruption notices arriving over a few
minutes. Each replica is a separate process under its own unprivileged uid, with its own empty
`HOME`/`TMPDIR` as working directory; `/app` is read-only to it, and replicas share nothing but
the queue and the cluster API. Replicas get SIGTERM at the end of the run. See `/app/README.md`
for running replicas against `devcluster` locally.
