# Edits lost after closing documents (docserver)

Support has a steady trickle of "my changes disappeared" reports. The pattern: a user edits a
document, closes it, and on reopening the edits are gone. It gets much worse during Valkey
failovers. During last Tuesday's ElastiCache maintenance, every document closed in a ~40 s
window came back without its recent edits. Postgres was healthy throughout.

What we see in the logs of the instance that had the user's edits: at the close, the store lock
was not acquired, then `store of <doc> skipped: Another instance is already storing this
document`, then `unloaded <doc>`. No row write. Separately, after the RDS maintenance window, a
few documents sat in memory with zero connections until the dashboard alert fired. Those logs
show an error while storing (a connection error) and nothing after it.

The service is `/app` (see `/app/README.md` for how it is deployed and operated). Please fix
docserver so that closing a document no longer loses what users typed, under lock contention, a
Valkey outage, or a reset database connection. Closed documents still need to be out of memory
within the unload budget.

Constraints:

- Several docserver instances run against the same Valkey and the same `documents` table, and
  they store the same documents. Each instance is a separate process with its own working
  directory: local files are not shared between instances.
- Keep the CLI (`python3 -m docserver --instance-id … --http-port …`), the environment variables
  (`PG_DSN`, `VALKEY_URL`, `KEY_PREFIX`) and the HTTP API as they are.
- The `documents` schema belongs to the platform team. docserver's database role can only read,
  insert and update `documents` rows.
- The store lock key is shared with the rest of the fleet, not only with docserver instances.
  Never delete or overwrite a lock someone else set.

`python3 /app/dev/devstack.py up` gives you a dev Postgres and Valkey on the local AWS endpoint
to run instances against (see the README).
