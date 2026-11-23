# partsink — the archiver's S3 part-file sink

`partsink` is the sink of our streaming archiver jobs (order-events, payments-audit, ...). The
output file is one S3 multipart upload, and every checkpoint captures enough S3 state to resume
that upload exactly where the checkpoint was taken.

```
sink.py              one attempt of the sink task: CLI + line protocol (below)
partsink/bucket.py   the in-progress part file: start/restore, write, checkpoint, completion
partsink/writer.py   S3 recoverable writer: multipart upload, parts, tail objects, recover
partsink/recoverable.py   the checkpointed handle (S3Recoverable)
jobmanager.py        local stand-in for the job manager (same protocol, layout and retention)
OPERATIONS.md        restore runbook and the storage audit
```

## S3 layout

All jobs share one archive bucket; each job owns everything under `jobs/<job>/` and nothing else.

| key | written by | what |
|---|---|---|
| `jobs/<job>/out/part-0` | sink | the part file: one multipart upload per fresh job start, resumed by every restore; completed by the publisher after the job stops |
| `jobs/<job>/out/_part-0_tmp_<uuid>` | sink | tail objects: the buffered bytes (< 5 MiB) that were not yet in a part when a checkpoint was taken |
| `jobs/<job>/chk/<id:08d>.json` | job manager | checkpoint manifests |
| `jobs/<job>/savepoints/*.json` | job manager | savepoint manifests |

S3 requires every part of a multipart upload except the last to be at least 5 MiB, which is why
the remainder at a checkpoint goes into a separate tail object rather than into a part.

A manifest is `{"job", "checkpoint_id", "attempt", "state"}` (savepoints add `"savepoint": true`);
`state` is the sink's snapshot, an `S3Recoverable`:

```json
{"key": "jobs/order-events/out/part-0", "upload_id": "…",
 "parts": [{"PartNumber": 1, "ETag": "\"…\"", "Size": 5242880}],
 "bytes_in_parts": 5242880, "tail_key": "jobs/order-events/out/_part-0_tmp_…", "tail_len": 3145728,
 "offset": 8000}
```

`offset` is the number of records the snapshot contains; the source resumes from it on restore.
`tail_key` is `null` (and `tail_len` 0) when the checkpoint fell exactly on a part boundary.

## The job manager

Production runs the sink under our job manager; `jobmanager.py` reproduces its behaviour locally.

- **Checkpoints.** Every `--checkpoint-every` records the job manager sends `CHECKPOINT <id>`.
  Ids increase and are never reused; a checkpoint that does not complete leaves a gap. Once every
  operator of the job has acknowledged checkpoint `n`, the job manager writes `chk/<n>.json` and
  then sends `COMPLETE n`. A snapshot line longer than 4096 bytes is declined, which fails the job.
- **Retention.** After writing `chk/<n>.json` the job manager deletes the manifests of all but the
  2 most recent checkpoints (`num-retained = 2`).
- **Savepoints.** Before upgrades and migrations an operator takes a savepoint of a completed
  checkpoint (`--savepoint-after <id>` locally). The savepoint manifest is a copy of that
  checkpoint's manifest under `savepoints/`; it names the same S3 state and is kept until an
  operator deletes it by hand, usually weeks later.
- **Completion notifications are asynchronous.** In production the sink is one of several
  operators, so `COMPLETE n` is not tied to the sink's latest snapshot: the sink may already have
  been sent `CHECKPOINT n+1`, and answered it, before `COMPLETE n` arrives, while checkpoint
  `n+1` is still waiting for other operators (its manifest is written later, or never, if the
  job fails first). A notification can also be lost entirely when the job fails, or finishes,
  right after a checkpoint completes. (The local stand-in happens to send `COMPLETE n`
  immediately.)
- **Failure and restart.** When any task fails, the job manager fails the attempt and starts a new
  one (a new process, the next `--attempt` number of this submission of the job) restored from
  the most recent retained checkpoint; with no checkpoint yet, the job starts fresh. Restarts are
  unlimited with a fixed delay. Before the job runs, every operator is opened; if any of them fails to open (in
  production: the broker is unreachable), the job manager sends `OPEN fail`, the attempt exits,
  and the next attempt restores the **same** checkpoint again. During a broker outage that can
  repeat many times.
- **Processes.** Every attempt runs in a fresh working directory with a fresh `TMPDIR`; nothing on
  local disk survives from one attempt to the next.
- **Lost task managers.** The job manager does not wait for a failed attempt's process to exit.
  When a task manager stops answering heartbeats (in production usually a network partition,
  now and then a VM that froze for a few minutes), the job manager declares its attempt failed
  and starts the next one after the restart delay, while the old sink process may still be
  running on the cut-off host. A partition or a freeze stalls that process's S3 requests too, so
  the old process carries on from wherever it stopped once the host is back, which can be in the
  middle of its startup, before it has restored or created anything, while the next attempt is
  already running. The old attempt gets no further commands, and nothing it sends can become a
  checkpoint. When the host reconnects, possibly minutes later, its task manager cancels the old
  attempt: it closes the process's stdin, sends `SIGTERM` if the process is still running 10 s
  later, and `SIGKILL` 5 s after that. A host can be lost for good at any time, also after it
  came back; it takes its process with it, as if killed, and no cancellation reaches it. (The
  local stand-in kills a failed attempt outright before starting the next.)

## Sink protocol

```
python3 /app/sink.py --bucket <bucket> --job <job> --attempt <n> [--restore-from <manifest key>]
```

Lines on stdin/stdout (stderr is for logs). The job manager may kill the process at any line
boundary.

| job manager sends | sink answers |
|---|---|
| *(start, no `--restore-from`)* | `STARTED <upload_id>` after creating the multipart upload for `out/part-0` |
| *(start with `--restore-from`)* | `RESTORED <checkpoint_id>` after recovering the in-progress file from that manifest; exit 2 if it cannot |
| `OPEN ok` / `OPEN fail` | `OPENED` / exit with code 3 |
| `WRITE <n>` + n record lines | `WROTE <offset>` (records written so far) |
| `CHECKPOINT <id>` | `SNAPSHOT <id> <state json>` |
| `COMPLETE <id>` | `COMPLETED <id>` once the sink has finished acting on the notification |
| `END` (the job has finished; its final checkpoint's manifest is written) | exit 0 |

Records are 1 KiB lines (1023 printable bytes and `\n`).

## Running locally

The S3 endpoint is `http://localhost:4566` (`AWS_ENDPOINT_URL` is set; credentials `test`/`test`).

```
python3 /app/jobmanager.py --bucket archive-dev --job order-events
python3 /app/jobmanager.py --help
```
