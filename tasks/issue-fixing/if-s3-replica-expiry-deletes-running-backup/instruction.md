A nightly restore check found missing files in a backup that the primary had
reported as successful. The repository is shared by a primary doing full backups
and a standby running maintenance; both jobs exited successfully. See
`/app/incident.log` and the repository docs in `/app`.

Repair the maintenance command so that this deployment can keep using it safely.
Keep count retention and failed-backup space reclamation working within a single
run, including when the primary has stopped mid-backup. Successful backups and
resumable work must remain intact. We cannot serialize the jobs operationally or
change the primary's protocol.

The failover controller also runs `repoctl.py reconcile` after losing a
maintenance host. That recovery command must honor completed maintenance and
preserve the restore inventory, including when a delayed old worker resumes.

The entry point is `python3 /app/repoctl.py expire --bucket NAME --retention-full N`.
Use the endpoint in `AWS_ENDPOINT_URL`; the local AWS service is already running.
`/app/README.md` covers ordinary setup and commands. You may change the Python
implementation, preserving the documented CLIs and repository format, including
its allowed extensions. Any pause announcements used by the starter must remain
supported.

Roles run in separate processes and fresh working directories with
`PYTHONPATH=/app`. They share S3, not local files or host locks. Maintenance must
return without waiting for a paused primary; don't change bucket versioning or
lifecycle settings as part of this repair.
