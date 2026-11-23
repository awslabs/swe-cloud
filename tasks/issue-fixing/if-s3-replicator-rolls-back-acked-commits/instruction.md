# strata backups lose acknowledged commits

`strata` (in `/app`, start with `/app/README.md`) is the continuous S3 backup behind our
single-writer record store: the writer node ships every commit into the current generation in the
bucket, rolls over to a new generation at each checkpoint, and `node restore` rebuilds a disk from
the bucket when a volume is lost. Four things came out of the last round of failure drills and the
backup audit.

**1. Empty restore after a crash.** A writer was SIGKILLed a moment after a checkpoint, while the
new generation's snapshot was still uploading. Its volume was replaced and `node restore` printed

```
{"generation": "e4cf0f6a-8e1b-7c6d-9a51-3c0f2b7d4e19", "seq": 0, "head": 0, "ids": []}
```

with exit code 0. The newest generation in the bucket held a single 36-byte `.dep`; the one listed
after it had all the frames. The restore log said
`Restoring from generation e4cf0f6a-…: action=ReuseGeneration(e4cf0f6a-…), recovered=False`.

**2. Old volume rolled the database back.** A node was started on a volume restored from an older
volume backup. It came up healthy and took writes, but afterwards a plain `node restore` was missing
commits that had been acknowledged before the old volume was attached. That bucket has not been
touched since.

**3. Broken links.** The audit flags generations with no `.dep` in several buckets, most of them
created right after a node restart. Those databases are live, and their buckets are what a
replacement node restores from. The object store also answers the odd request with
`503 SlowDown`.

**4. Stuck after throttling.** During a throttling episode on the store, a writer started
answering every `/checkpoint` with `503` and kept doing so until it was restarted; the generation
it was writing into never got its snapshot.

## What we need

- An acknowledged commit (a `200` from `/commit`) is never lost: not by a crash at any moment,
  including while a snapshot is uploading, and not by a node booting on an old disk.
- `node restore` never reports success with an empty or older state than the bucket holds; if it
  cannot build a consistent state it says so and exits 3.
- A node booted on a disk that lags the bucket comes up serving the bucket's newest state and never
  publishes older state into the bucket.
- What works today keeps working: checkpoints and commits stay within the operating limits in the
  README even while a snapshot upload takes minutes, and again as soon as a throttling burst has
  passed; the bucket layout stays exactly as documented (other tools read it); and the CLI and
  HTTP interface stay as they are.

The fix goes into `/app`. It is exercised as `python3 /app/node serve ...` and
`python3 /app/node restore ...`, with `S3_ENDPOINT` pointing at an S3 endpoint in front of the
emulator on `localhost:4566`, each process in its own fresh working directory with its own `HOME`
and `TMPDIR`: the bucket and the process's own `--db-dir` are the only things that carry over from
one process to the next.
