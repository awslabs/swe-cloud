# Same render job taken twice while an instance still holds it

`/app` is the SQS relay (see `/app/README.md`). It pulls render jobs off a standard SQS queue, 20 per
poll, and hands them one at a time to the render farm's intake, deleting each message once the
intake has accepted it. Several relay instances consume the same queue, each its own process;
they share nothing but the queue.

Since we went to 20 messages per poll, the farm keeps seeing the same job taken twice at the same
time: one instance receives a message that another instance is still handing to the intake, or
still has waiting in its batch behind a slow job — often several jobs of one batch at once. It is
worst in the quiet hours, and it spiked during the SQS latency event on 2026-10-03, when SQS API
calls took 10–20 s to answer for about an hour.

What on-call has seen and tried:

- Raising `VISIBILITY_TIMEOUT` to 300 s stopped the duplicates, but when an instance was OOM-killed
  its jobs then sat for minutes before anyone else picked them up. Rolled back. The platform keeps
  the timeout at 20 s and that is not up for change.
- During deploys (SIGTERM), jobs that a stopping instance had received but not started sometimes
  take much longer than the visibility timeout to show up on another instance.

Please fix the relay. Everything in the README's operating constraints still holds, and so do the
configuration variables and the poll shape. It is still started as `python3 -m relay` (with
`PYTHONPATH=/app`, from any directory) and configured through the environment as documented.

In this sandbox SQS is the local emulator at `http://localhost:4566` (credentials `test`/`test`,
region `us-east-1`), and `python3 /app/tools/sink.py` is a stand-in for the intake.
