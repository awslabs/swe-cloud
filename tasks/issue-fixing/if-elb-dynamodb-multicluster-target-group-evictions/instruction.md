# INC-4471: `web` target group won't settle since cluster-b joined it

We moved cluster-b onto the `web` target group next to cluster-a for the blue/green rollout, and the
network team put their legacy hosts back in the same group. Since then the group won't settle:

- the legacy hosts were drained within seconds of the controllers starting, and the network team
  says they keep disappearing after they re-add them;
- pods from one cluster drop out of the group whenever the other cluster's controller starts up or
  restarts, then come back a moment later;
- when one of cluster-a's nodes died, some of cluster-a's own running pods dropped out of the group
  for several seconds after its controller came back.

Each controller acts as if the target group were its own. Last week cluster-a's controller was
OOM-killed several times in a row during a deploy, and the egress proxies were flapping during that
deploy too. Whatever we run has to come through that.

Please fix the controller in `/app` so the two clusters and the legacy hosts can share the group. Each
cluster's controller keeps that cluster's endpoints registered, takes its own removed pods out of the
group, and leaves every other target alone. `/app/README.md` describes the deployment as it now
stands: the shared group, the ownership table provisioned for this rollout, the egress policy, and
how the controllers are supervised and restarted. All of it must still hold after your change.

Keep the command line and the binding file format as they are. In production each cluster's controller
runs as `python3 /app/controller.py ...` with `PYTHONPATH=/app`, as its own process in its own empty
working directory, with `--aws-endpoint` pointing at that cluster's egress proxy. For local work,
Floci answers on `http://localhost:4566`, and the README shows how to create a target group and the
table there.
