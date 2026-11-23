# strata

Continuous S3 backup for a single-writer record store. `strata` is our Python port of libSQL's
*bottomless* replicator: the writer node keeps its database on a local volume and ships every
commit to an S3 bucket, so a node can be rebuilt from the bucket alone when its volume is lost.

```
/app/node            entry point (python3 /app/node serve|restore ...)
/app/strata/         the replicator, the HTTP front end, the CLI
```

## Generations

A database `NAME` is stored as a series of **generations**. A generation is one key prefix
`NAME-GEN/`, where `GEN` is a UUIDv7 built from an *inverted* timestamp, so the newest
generation of a database is the first one S3 lists under `NAME-`. The writer starts a new
generation at boot and at every checkpoint, and from then on ships its commits into it, numbering
frames from 1 again.

A generation that is not the database's first records its parent in `.dep`. Its snapshot
(`db.sqlite`, then `.changecounter`) is uploaded in the background after rollover. A restore walks
`.dep` back to the nearest snapshot (or to the database's first generation) and replays every
generation's frames forward to the newest one.

## Bucket layout (fixed)

Other tooling reads these buckets directly (the backup audit, the fork and point-in-time tools,
retention), so the layout below is a format contract, not an implementation detail.

| key under `NAME-GEN/` | content |
|---|---|
| `.dep` | the parent generation id, 36 ASCII characters |
| `db.sqlite` | SQLite snapshot: `rows(id INTEGER PRIMARY KEY, payload TEXT)`, `meta(k TEXT PRIMARY KEY, v INTEGER)` with `k='seq'` and `k='head'` |
| `.changecounter` | ASCII decimal `seq` of that snapshot; written only after `db.sqlite` is stored |
| `{first:012}-{last:012}-{unix_ts}.jsonl` | a frame batch: one JSON line per frame, `{"frame": f, "seq": s, "id": n, "payload": p}`; frames are contiguous from 1 within a generation |

- These are the only objects a bucket holds. No other keys, under a generation or beside them.
- Objects are written once and never overwritten (retrying a PUT that failed is fine). Neither
  the node nor `node restore` deletes objects; retention owns deletion.
- Every object other than `db.sqlite` is at most 4096 bytes.
- `seq` counts commits: commit number `s` of the database's history carries `seq = s`.
  `head` is the id of the last commit.
- One plain `PutObject` per object, path-style addressing, no `If-Match`/`If-None-Match`.

## Running

The node talks to S3 only through `S3_ENDPOINT` (in production a regional gateway; here an
endpoint in front of the local emulator on `http://localhost:4566`), with
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_DEFAULT_REGION` from the environment.

```bash
export S3_ENDPOINT=http://localhost:4566
aws --endpoint-url $S3_ENDPOINT s3 mb s3://backups
python3 /app/node serve --db-dir /tmp/n1 --bucket backups --db db1 --port 7000 &
curl -s localhost:7000/health
curl -s -XPOST localhost:7000/commit -d '{"id": 1, "payload": "hello"}'
curl -s -XPOST localhost:7000/checkpoint -d '{}'
kill -TERM %1
python3 /app/node restore --db-dir /tmp/r1 --bucket backups --db db1
```

### `node serve --db-dir DIR --bucket B --db NAME --port P`

Boots from `DIR` (created if missing) and the bucket: decides whether the local disk is current,
restores from the bucket, or reuses a generation, then starts a generation to write into. A node
booted on a disk that lags the bucket (a volume restored from an older backup, a replaced machine)
must come up with the bucket's newest state; it must never publish older state over newer.

- `GET /health` → `200 {"generation": G, "seq": s}` once booted.
- `POST /commit {"id": n, "payload": "..."}` → `200 {"seq": s}`. Inserts row `(id, payload)` and
  sets `head := id`. The 200 is the acknowledgement: it is sent only after the frame batch carrying
  the commit is stored in S3 under the current generation. Any other answer means the commit may
  or may not be in the database afterwards. Payloads are up to 1024 characters.
- `POST /checkpoint` → `200 {"old": G_old, "new": G_new}`. Rolls over to a new generation and starts
  its snapshot upload. A checkpoint never waits for that upload.
- `SIGTERM`: drains pending uploads and exits 0. `SIGKILL` is how machines die.

### `node restore --db-dir EMPTYDIR --bucket B --db NAME`

Rebuilds a disk in `EMPTYDIR` from the bucket's newest generation, reading only the bucket (it never
writes to it, and it runs while a live node may be writing). `node serve` can boot from the result.
Prints one JSON line `{"generation": G, "seq": s, "head": h, "ids": [...]}` and exits 0, or exits 3
with a message when the bucket holds no consistent chain to restore from. A restore that completes
must hold every acknowledged commit; an empty or partial result with exit 0 is a silent data loss.

Each `serve` and `restore` runs as its own process, possibly on another machine: the bucket and its
own `--db-dir` are all a process can rely on. Nothing in a local temp directory, `$HOME` or another
node's directory survives to the next one.

## Operating limits

| | |
|---|---|
| commit acknowledged | < 2 s |
| `/checkpoint` returns | < 2 s |
| boot to healthy (including any restore) | < 15 s |
| `SIGTERM` to exit | < 15 s |
| `node restore` | < 30 s |

Snapshot uploads of real databases take minutes, and commits keep flowing into the new generation
while they run; only the *next* checkpoint waits for the previous generation's snapshot.

The object store answers the odd request with `503 SlowDown` (the request was not performed; sent
again a moment later, it normally goes through), and now and then throttles a prefix in bursts
that last several seconds while it rebalances. The regional gateway in front of it answers
`504 Gateway Timeout` when the store is slow to reply; the store may still complete that request.
An odd `503` is routine and fails neither a commit nor a checkpoint; commits and checkpoints sent
during a burst may fail. Once a burst has passed, the node takes commits and checkpoints within
the limits above again, without a restart.
