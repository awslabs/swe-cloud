# tile-log worker

A sequencing worker for an append-only, Merkle-tree log ("the log").
Submissions arrive over HTTP, each carrying one or more entries; the
entries of a submission are sequenced together, and a successful
submission is answered with the index its first entry received and the
checkpoint that includes them.  Certificate Transparency vocabulary is
kept to a minimum: entries are opaque payloads, tiles are the objects
readers fetch to recompute the tree.

## Running a worker

```sh
python3 /app/sequencer.py --config /path/to/config.json
```

A sample config is in `config.sample.json`.  Fields:

| field | meaning |
|---|---|
| `worker_id` | label this worker uses about itself (logs, control channel) |
| `origin` | first line of every checkpoint of this log |
| `table` | DynamoDB table holding the checkpoint record |
| `log_id` | 32-byte log identifier, hex; partition key of the record |
| `bucket`, `prefix` | object bucket and key prefix for this log |
| `ddb_endpoint`, `s3_endpoint` | service endpoints |
| `listen_host`, `listen_port` | where this worker serves HTTP |
| `control_socket` | Unix socket for the control channel (empty: no pauses) |
| `checkpoint_time_step` | logical clock increment per committed checkpoint |
| `tile_width` | entries per full tile (4; also `treekit.TILE_WIDTH`) |

Credentials and region come from the environment.  Each worker runs in
its own working directory; nothing about a log's state is kept on
local disk, and two workers never see each other's files.  Whatever
must survive a worker, or be shared between workers, lives in the
table or the bucket.

## HTTP interface

* `GET /health` — readiness only.  It is not evidence about the log.
* `POST /submit` — body `{"request_id": "...", "entries":
  ["<base64>", ...]}`, at least one entry.  The entries are sequenced
  together: they become the next entries of the tree, in order, under
  one checkpoint.  Responses:
  * `{"status": "committed", "index": N, "time": T, "checkpoint": "<b64>"}` —
    the first entry of the submission is at index `N` of the returned
    checkpoint, and the entries that follow it occupy the indexes that
    follow.
  * `{"status": "conflict"}` — the attempt did not commit.

One submission is one attempt: the attempt keeps the checkpoint it
started from until it commits or fails.

## State layout

**Checkpoint record (DynamoDB).** One item per log:

```
logID       (B)  the configured 32-byte log id
checkpoint  (B)  the complete checkpoint bytes
```

The table holds checkpoint records only; entry and tile bytes are not
stored there.  The record is the authority for what has been committed
and is meant to be advanced with a conditional write, read with
strong consistency, and to remain usable across processes and
restarts.

**Objects (S3), under the prefix.** Public, fixed coordinates:

* `checkpoint` — the published checkpoint: what readers take as the
  head of the log.
* `tile/data/<x>.p/<w>` — data tile `<x>`, `w` entries, `w < 4` (full
  tiles drop the suffix).  Bytes: canonical JSON, one array of
  base64 payloads in index order (`treekit.encode_data_tile`).
* `tile/<L>/<x>[.p/<w>]` — hash tiles: `w` raw 32-byte hashes; level 0
  holds leaf hashes, level `L` holds one root per `4**L` leaves.

Because the coordinates are fixed, the objects covering any tree of a
given size are the same objects for every writer: `treekit` in this
directory is the codec for all of them, and the formats above are part
of the deployed contract — operators' tooling reads these keys.

**Checkpoint bytes.** Exactly four lines, ASCII:

```
<origin>
<tree size>
<root hash, 64 hex chars>
<logical time>
```

The first checkpoint of a log has size 0 and time 0; every committed
successor adds `checkpoint_time_step` to the time.  The clock is
logical, not wall time.  Two checkpoints are the same checkpoint only
if their bytes are equal.

## Control channel

With `control_socket` set, the worker announces each pause below on
that socket as `{"pause": ..., ...facts}` and then blocks until the
driver acknowledges.  The pauses are part of the contract: a worker
must keep announcing all of them, under the same names, with the same
facts, at the same points in its flow.

| pause | point in the flow | facts |
|---|---|---|
| `startup_complete` | after loading the log's state at startup, before serving | `worker`, `checkpoint` (b64), `size`, `root` |
| `batch_assembled` | after an attempt has fixed its parent checkpoint and its candidate, before it writes anything for the attempt (reading the record or the published tree, or repairing an already-committed tree, can come before it) | `worker`, `request_id`, `parent` (b64), `parent_size`, `index`, `candidate_size`, `candidate_root`, `candidate_time` |
| `before_reply` | immediately before responding to a submission | `worker`, `request_id`, `status`, `index` (committed only) |

## Library modules

* `treekit.py` — hashing, tile paths and encodings, checkpoint
  serialization.  Deterministic; shared format.
* `lockdb.py` — the checkpoint record client (`fetch`, `create`,
  `replace`; `replace` raises `ConflictError` when the stored bytes are
  no longer the saved ones).
* `objstore.py` — plain object put/get/delete under the prefix.
* `control.py` — the control channel.
