# Steps stall for 10+ seconds after Valkey connection blips

From the on-call channel:

> A few times a day a run's next step sits for 10–13 s before it starts, well past our 3 s dispatch
> SLO. Every time, one executor logged `lease call failed item=<id> lease_id=None err=TimeoutError(...)`
> (sometimes a `ConnectionError`) right when the item became due, and a little later some executor's
> scavenger logged `scavenged lost items`. Once something picks those items up they run fine. It lines
> up with the periods when the shared shard is slow or connections get reset.

stepq lives in `/app`; `/app/README.md` describes the queue, the function library, how to run things
locally and the operating notes we hold it to. Please fix it so that an item whose lease call runs into
a connection problem still starts promptly. Everything in the README's operating notes must keep
holding: an item never runs twice, nothing runs while another executor or the batch fleet holds it, a
dead executor's items are picked up within the SLO, and the batch fleet keeps working against the
library's documented functions.

We will check your change by running several executors (`python -m stepq.executor --id ...`), each as
its own process from its own empty working directory with `PYTHONPATH=/app`, against a fresh queue
cluster whose connections stall, reset and lose replies the way the README describes, whose hosts can
die, whose executors get restarted by deploys, and whose step runner sits behind the load balancer the
README describes. The executors share nothing but Valkey. Keep the CLI, the environment variables and
the `READY` line as documented.
`python -m stepq.devstack` gives you a local cluster and step runner.
