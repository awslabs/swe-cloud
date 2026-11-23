# signup queue: users stuck "deduplicated" after the Postgres outage

During Tuesday's Postgres outage the connection pool was exhausted and every `signup` handler sat
waiting for a connection. Worker health checks failed, so the supervisor `kill -9`'d and replaced
workers several times; jobs stalled, were picked up again, hung again, and some were retried. While
trying to get things moving, on-call also cleaned up a handful of jobs by hand with `redis-cli`:
some job records (`dq:signup:job:<id>`) were deleted, some ids were taken out of the wait or active
list, some both.

When Postgres came back the queue drained: nothing waiting, active or delayed. But for a few dozen
users every `dq add` came back `"deduplicated": true` against an old job id, and nothing ran for
them again. Deleting their `dq:signup:de:<email>` keys by hand unstuck them. Support also found
users whose last account sync (added with `--keep-last`) from during the outage never ran at all,
although the add had come back deduplicated against a sync that was running at the time. We don't
want to be doing any of this in the next incident, and there will be one: workers will get killed
again and someone will clean up jobs by hand again.

Please fix `dq` (in `/app`, see `/app/README.md`) so users can't end up stuck like this again,
without loosening deduplication: everything README promises about it must still hold. In
particular a user must never have two of these jobs running at once, throttle windows
(`--ttl-ms`) keep behaving as documented, and an account sync must never run with older data than
one that already ran.

Things to keep in mind:

- `python3 -m dq add` runs from the web tier as short-lived processes, many at the same time, and
  whether or not any worker is up (during the outage there were minutes with no worker at all).
- Workers are killed with `SIGKILL` whenever the supervisor decides they're unhealthy.
- Every producer and worker is started on its own, in its own working directory; nothing is shared
  through local disk.
- Ops tooling relies on the command lines, the JSON line `dq add` prints, the worker's
  `READY <pid>` line and the key layout in the README. Keep them.

To try things, `eval "$(python3 tools/dev_cluster.py)"` gives you a fresh ElastiCache (Valkey) node
on the local AWS endpoint and exports `DQ_REDIS_URL`.
