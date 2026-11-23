# Standby maintenance

We schedule expire on the standby to move repository listing/deletion load away
from the primary. Jobs can overlap, including two maintenance jobs on different
standbys. The host controller may kill a maintenance process without warning,
after any S3 request, and fail it over to a fresh host. The replacement first runs
`repoctl.py reconcile`, then resumes the expire schedule with the current policy.
A delayed old worker can resume while that replacement is running; neither may
revive a retirement published by the other. Retention settings can be raised
between jobs. A returned catalog is used directly as the restore inventory, so
it must not advertise a backup whose files maintenance has removed.

A primary can be suspended for an arbitrary time while copying data or publishing completion, and a failed primary can leave
its newest backup for a replacement to resume. The next backup handles that work;
expire must finish without requiring the replacement to start. A normal maintenance
invocation on this small repository has a 30-second operational deadline.

Hosts have separate local filesystems and local locks. No primary code consults
an S3 lock object, a queue, or another AWS service before writing its manifest or
registering its backup. Only the repository objects are shared. Bucket versioning
and lifecycle are managed by the infrastructure team; maintenance leaves them
unchanged. S3 object deletion is permanent for this deployment.

The endpoint can be an HTTP gateway in front of S3. All repository requests must
use AWS_ENDPOINT_URL, including retries. S3 makes each individual key atomic and
its listings strongly consistent; it does not give a transaction across the
manifest, the data objects and backup.info. ETags are opaque conditional-write
validators, not application timestamps.

Local emulator note: Floci 2.2.0 supports PutObject If-Match on an existing key.
After a rejected conditional PUT (HTTP 412), discard that boto3 client before the
next request: its keep-alive connection may return a spurious 400/405. The primary
already does this. Create-only If-None-Match and conditional CopyObject are not
supported in this venue. Use ordinary per-object deletion; no batch delete or
prefix-delimiter behavior is required by the service.
