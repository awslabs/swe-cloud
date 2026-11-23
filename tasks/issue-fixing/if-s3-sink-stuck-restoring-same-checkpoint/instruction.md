# order-events archiver stuck in recovery after the broker outage; storage audit failing again

**From:** data-platform on-call  **Component:** partsink (`/app`)

Last night the order-events broker was unreachable for about twenty minutes. The archiver job
failed and the job manager restarted it from the latest retained checkpoint, as it is supposed
to. While the broker was still down those restarts failed in open, which is expected. Once the
broker was back, every further restart died while restoring the sink:

```
21:47:02 a7 partsink.sink ERROR could not initialise the in-progress part file
Traceback (most recent call last):
  ...
partsink.writer.RecoverableStateMissing: tail object jobs/order-events/out/_part-0_tmp_3f0c9b2e-… is gone (NoSuchKey)
```

The job never came back on its own; we had to cancel it and start it fresh, and lost what was in
flight. A sink that cannot be restored from a checkpoint it handed to the job manager is data loss.

Separately, the post-incident storage audit (see `OPERATIONS.md`) flagged the archiver's prefix
again: objects that no checkpoint or savepoint references, and open multipart uploads we keep
paying for, all from around the restarts. The sink's cleanup was added because of exactly that
kind of pile-up, so turning cleanup off is not a fix.

Please fix the sink so that it copes with everything the job manager does (`README.md` describes
its behaviour) and with our restores by hand (`OPERATIONS.md`), keeps every checkpoint and savepoint that the job manager or the runbook may
restore from restorable, and leaves the job's prefix clean once the job stops, including whatever
failed attempts left behind.

Constraints:

- Keep `sink.py`'s command line, the line protocol and the snapshot/manifest format as they are;
  the job manager and the restore tooling depend on them.
- Every attempt is a new process in a new working directory; nothing on local disk carries over
  between attempts.
- The archive bucket is shared with other teams' jobs.

To try it: the S3 endpoint is already running at `http://localhost:4566` (credentials are in the
environment). `python3 /app/jobmanager.py --bucket archive-dev --job order-events` runs a job end
to end; `--help` lists the options.
