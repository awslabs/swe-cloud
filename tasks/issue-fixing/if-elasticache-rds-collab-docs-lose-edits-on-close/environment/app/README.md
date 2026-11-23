# docserver

The collaborative document server. Clients edit shared documents. Each document is a Yjs doc
whose `edits` map holds one entry per edit. Python port of our Hocuspocus deployment: the hook
chain, the Redis (Valkey) extension and the Database extension keep their upstream shapes.

```
docserver/
  __main__.py    CLI: docserver --instance-id I1 --http-port 7101
  http_api.py    HTTP/JSON control API (stands in for the websocket provider)
  server.py      document registry: open, edit, close, store, unload
  hooks.py       extension hook chain
  document.py    the in-memory Yjs document (pycrdt)
  redis_ext.py   Valkey: update fan-out between instances, the store lock
  redlock.py     Redlock (port of @sesamecare-oss/redlock 1.4.0)
  database.py    Postgres: load a document from its row, store it back
schema.sql       the documents database (owned by the platform team)
dev/devstack.py  a dev Postgres + Valkey on the local AWS endpoint
bin/docserver    wrapper for python3 -m docserver
```

## How it is deployed

- docserver runs as a **fleet of instances** (six in production) behind a load balancer. All of
  them share one ElastiCache Valkey and one RDS Postgres `documents` database. A document can be
  open on several instances at once.
- Each instance is its own process on its own host. Instances share no local disk and no memory.
  Valkey and Postgres are the only things they have in common.
- An instance loads a document from its `documents` row on the first open. While it has the
  document loaded it publishes its local updates on `{KEY_PREFIX}:{doc}` and applies the updates
  other instances publish. Fan-out is plain Valkey pub/sub. It is best effort: a message published
  while an instance is disconnected or reconnecting is never redelivered. An instance can therefore
  hold edits that another instance holding the same document never received.
- When an instance's last connection to a document closes, the instance stores the document and
  unloads it. Each instance stores only for its own connections; nothing else persists the edits it
  acknowledged.

### Store lock

Before a document is stored, the Valkey extension takes a lock (`{KEY_PREFIX}:{doc}:lock`, the
Redlock acquire script, one attempt) so that only one instance stores it at a time. If another
instance holds the lock, the store is skipped. The lock expires after `lock_timeout_ms` (default
`1000`). It is released with a compare-and-delete when the store finishes.

The lock key is shared with every other instance in the fleet, and with doccompact (below). Take
it only with the acquire script (`SET NX PX` semantics), and extend or release only a value you
set.

### Row compaction (doccompact)

The Yjs state of a long-lived document keeps growing, so the platform team runs `doccompact`, a
background job that rewrites rows to keep them small. For each document it takes the store lock
(the same `{KEY_PREFIX}:{doc}:lock` key, `SET NX PX 10000` with its own value), reads the row,
re-encodes the state, writes it back with `version + 1` and releases the lock with a
compare-and-delete. A pass on one document can hold the lock for several seconds, never more than
its 10 s PX. doccompact relies on the store lock to keep every other writer off the row while it
works: it does not check that the version is still the one it read. When the lock is taken it
leaves the document and tries again later. It connects to Postgres as the `doccompact` role.

### The documents table

See `schema.sql`. One row per document. `state` is the full Yjs update of the document.
`version` is 1 when the row is created and goes up by one on every committed write. The platform
team keeps `documents_history`, every committed version of every row, for audits and for
restoring a single document. docserver connects as the `docserver` role. That role can `SELECT`,
`INSERT` and `UPDATE` rows of `documents` and nothing else: no DDL, no other tables. The schema
is not ours to change.

## Operations

- **Unload budget.** A document whose last connection has closed must be persisted and out of
  memory within **15 seconds**. Instances run close to their memory limit. The fleet dashboard
  alerts on any document that is still loaded with zero connections after that.
- **Valkey** is a single ElastiCache node. Failovers and maintenance make it unreachable for
  anything from a few seconds to about a minute. Postgres is usually healthy throughout.
- **RDS** maintenance and failovers reset open connections, and for a few seconds afterwards new
  connections are refused. A statement in flight fails with a connection error.
- `GET /healthz` must answer 200 for the life of the process, Valkey or no Valkey. The
  orchestrator restarts an instance that fails it, which drops whatever that instance held in
  memory.

## HTTP API

| request | response |
|---|---|
| `POST /open {"doc": name}` | 200 once the document is loaded and the connection is counted |
| `POST /edit {"doc": name, "edit_id": id}` | 200 once the edit is applied in memory: this is the acknowledgement the client gets |
| `POST /close {"doc": name}` | 202 immediately; the last close stores and unloads |
| `GET /docs/{name}` | `{"loaded": bool, "connections": int, ...}` |
| `GET /healthz` | 200 |

An edit `id` becomes the key (and value) of the `edits` map entry.

## Running it locally

```bash
eval "$(python3 /app/dev/devstack.py up)"        # dev Postgres + Valkey, schema applied, env printed
/app/bin/docserver --instance-id I1 --http-port 7101 &
/app/bin/docserver --instance-id I2 --http-port 7102 &
curl -s -XPOST localhost:7101/open  -d '{"doc":"notes"}'
curl -s -XPOST localhost:7101/edit  -d '{"doc":"notes","edit_id":"notes:I1:1"}'
curl -s -XPOST localhost:7101/close -d '{"doc":"notes"}'
curl -s localhost:7101/docs/notes
python3 /app/dev/devstack.py show notes          # the row and its history, decoded
```

The master connection string for the dev database is printed on stderr by `devstack.py up`.
