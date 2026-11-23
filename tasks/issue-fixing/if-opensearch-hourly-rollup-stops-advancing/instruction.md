The metrics team's hourly rollup has stopped advancing. The initial backfill looks right, and
scheduled runs keep reporting success, but the dashboard still shows old counts as incoming data
ages past the one-hour holdback. During target pressure, some batches also report rejected
items and replacements repeatedly write the same early buckets. Restarting has not cleared
the backlog. After a stalled worker is replaced, operators have also seen counts roll back
and the progress record revert when the old worker eventually resumes. The rolling dashboard
also retains expired hours and fully revoked device buckets, sometimes after archival has
already removed their source events.

Repair the Python service in `/app` so the hourly index keeps matching the selected source data.
Keep this an incremental service: the production read allowance rules out rerunning the backfill.
Late deliveries and corrections must keep working, as must recovery across target backpressure
and worker interruption, including safe replacement of an abandoned worker.
The existing entry point and stored document formats are used by operations; preserve them.

`/app/README.md` describes the deployment, invocation and operating contracts. You can provision
a development domain using `/app/admin.py`. AWS APIs are available at `AWS_ENDPOINT_URL`; the
venue already runs. Your final `/app` tree is the deliverable.
