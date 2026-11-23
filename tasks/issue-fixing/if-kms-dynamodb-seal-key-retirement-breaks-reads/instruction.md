Seal-key retirement has been rolled back twice this week. The migration command
reported success and normal reads looked healthy, but retiring the former KMS keys
made some stored secrets unreadable. Re-enabling those keys restored service.

Fix the Python seal service in `/app` so an operator can change its seal key,
complete an online migration, and safely retire all former keys. Keep existing
values readable and durable and keep accepting traffic during the operation.
The migrator must tolerate being killed and rerun; writers can remain running
across the change. Clients also report that reconnecting and retrying a put after
a lost response can bring back an older secret. Preserve the retry contract while
repairing retirement; request IDs survive reconnects and key changes. Do not
disable or delete keys as part of the repair.

`/app/README.md` describes the service and operations contract. Start the local AWS
endpoint with `bash /usr/local/bin/start_floci.sh`; `python /app/initialize.py`
creates an empty installation for local use. The entry point is `python -m seal`
with `writer`, `get` and `migrate` subcommands. Preserve that interface and the
stored envelope format. Each role runs in its own process and working directory;
shared durable coordination belongs in the AWS services, not local files.
All AWS traffic must continue to use the process's `AWS_ENDPOINT_URL`, including
any reimplementation, so the existing network fault instrumentation still works.
