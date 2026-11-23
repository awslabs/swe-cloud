Orders lag stopped moving during a worker rollout on our MSK cluster. The group still
looked healthy, and another restart didn't clear the backlog. We have also seen this
after a pod was killed rather than drained. Downstream receipts have also fallen
behind the committed input position during these deployments.

Repair the Python worker in `/app` so a deployment cannot leave part of a topic idle.
Keep the static membership behavior: routine restarts inside the session must preserve
the group's assignments and avoid rebalances. Abrupt loss is part of the deployment
model, too. The operational bounds and launch interface are in `/app/OPERATIONS.md`.

Use the real broker through Floci; `python /app/provision.py` creates a development
cluster and prints its bootstrap endpoint. Launch workers with `python /app/worker.py
--help`. Preserve the callback announcements and blocking behavior of the starter if
you replace it. Each worker incarnation runs in a fresh working directory; local files
are not shared between workers or carried across a restart. Fix the application under
`/app`; the venue helper and its lifecycle are supplied infrastructure.
