# Credential operations

Deployment owns the configured initial password until rotation has completed. From then on,
the most recent completed rotation owns the credential, including when a console update has
subsequently changed the secret. Deployment should restore that credential's version under
AWSCURRENT and record it as the deployment anchor. It must not roll back a rotation or accept
an arbitrary console value as the new source of truth. Credentials must keep their original
version identities: deployment must not clone them into fresh versions.

Rotation is an independently operated four-step process (rotation.py). It publishes a version
with AWSPENDING, records its token in the journal, installs the password in the downstream
service, tests it, and finally publishes AWSCURRENT. The journal completion acknowledgement
comes after the Secrets Manager publication. A worker can stop at any step. In particular,
a pending journal row does not tell an operator whether publication happened. Restarting the
rotator resumes its token rather than starting another rotation. AWSPENDING may remain on a
completed version. Deployment must leave that label alone and must never publish an unfinished
rotation on the rotator's behalf. During an unfinished rotation the database may already use
the pending credential; this is not evidence that publication has completed.

Secrets Manager automatically places AWSPREVIOUS on the version displaced by an AWSCURRENT
move. Do not clean up AWSPREVIOUS during deployment. A label move must name the displaced
holder in RemoveFromVersionId; it can fail if that holder has changed. Recoverable staleness
should result in a refreshed decision, not a failed deployment or a blind overwrite. Rotation
records and labels are separate service reads, not one transactional snapshot.

Versions with no labels can eventually be collected by the service. A missing deployment
anchor after rotation is a stale reference, not a request to reset the password. Bootstrap is
only for a new installation; it is not an outage recovery procedure. Keep the deployment
anchor aligned with the credential owner even if no label change is needed.

The downstream record models a database for this local installation; it stores a hash rather
than the password. Production database and Lambda runtimes are outside this port. Historical
version collection is also a service concern, not something the deployment program implements.

## Replacing an installation's secret

A resource address is an installation slot, not a permanent secret identity. Operators
sometimes replace its backing secret, and sometimes return it to a secret used earlier.
Rebinding must leave the departed secret, its history and its rotation records untouched.
For a destination with a rotation journal, use its credential owner; a destination with
no journal needs its configured initial deployment credential. A pointer into the departed
secret is not ownership evidence for the destination. Rotations of departed secrets may
continue independently.

The review queue is at-least-once and can deliver a plan after the resource has been
rebound. A completed replacement retires all plans reviewed before that binding change.
Delivery of retired work should acknowledge success without changing AWS resources. This includes
plans requesting a replacement that was once approved but has since been superseded,
and plans whose configured secret happens to match a later return to the same name.
Ordinary rotation and repeated deployment do not retire reviewed plans. Retirement
survives scheduler restarts.

Deployment workers can also be killed after an AWS write succeeds, before they receive
the reply or commit subsequent state. Retry of the same reviewed plan must finish an
interrupted replacement. It must not confuse unfinished work with a completed binding
change, create another initial version, or replay a completed replacement as a new one.
The scheduler runs at most one deployment at a time, including retries. Secrets Manager
request tokens identify idempotent version creation; a lost response does not mean the
service rejected the write. All of this recovery must survive loss of the worker's files.
