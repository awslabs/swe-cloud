# Incident brief: committed table versions reverted after their coordination entries were reclaimed

## What we observed

Two ingestion jobs started against the same table at about the same time; both had captured the
table at version 0 before either committed. From the object inventory and the downstream
consumers:

- **14:06** — version 1 committed and acknowledged; several downstream jobs consumed version 1's
  actions.
- **14:16** — version 2 committed and acknowledged.
- **15:29** — version 1 written **again**, by the slower of the two original jobs, which had been
  writing its data files all along and only reached its commit at the end.

Afterwards, version 1's object holds the slow job's actions. The actions committed at 14:06 are
nowhere in the log. Consumers that had already read version 1 silently saw its contents change
underneath them.

Nothing was reported wrong at the time: every writer, including the late one, believed it had
committed and logged no error. By the time we looked, the coordination table had no entry for
version 1 — which is expected here, since entries for completed versions are short-lived
bookkeeping and the reclaimer had already taken them. The version's object was still present in
object storage, carrying the wrong bytes. The service lives under `/app`; writers, readers and
the reclaimer are separate processes with separate working directories.

## What must hold afterwards

1. **Permanence.** Once a commit has been acknowledged, that version's object keeps exactly the
   acknowledged bytes for the life of the table. No writer may change them — however old the
   snapshot it started from. Reads of object storage here are strongly consistent.
2. **Collision reporting.** When two writers contend for the same version, exactly one wins. The
   loser is told `conflict` promptly, within seconds. A `committed` reply is a certificate: it
   must mean the version's object holds the caller's actions and the coordination entry has been
   marked complete. Anything that did not happen is reported as it happened.
3. **The coordination entries stay bookkeeping.** A completed entry carries an expiry 2 s after
   its completion; the reclaimer removes eligible entries, and a completed entry must really be
   gone within a few seconds of becoming eligible. Entries for unfinished versions carry no
   expiry and are never reclaimed, because another process may still have to finish them. When a
   table's writers and readers settle, the coordination table must hold nothing for that table.
4. **Recovery.** A writer that dies between reserving a version and publishing it leaves a
   recoverable state: its entry and its temp object survive, and another process — a reader
   listing the table, or the writer of the next version — finishes that publication from the
   recorded temp object, then carries on. A successor version must not be prepared while its
   predecessor is unfinished.
5. **Progress.** Commits that do not collide keep succeeding within seconds. This cannot be
   fixed by refusing, delaying or serializing writers: the writers are independent processes
   that share nothing but the services.

## What is not in question

The design — log objects and temp files in object storage, per-version coordination entries in
DynamoDB with conditional reservation, expiry only on completion. The command protocol and the
reply vocabulary. The storage channel in `storage.py` as the only supported way for store code to
reach S3 and DynamoDB; do not call the AWS SDKs from store code directly. And the operating
instrumentation: the store announces its pauses on the operations socket whenever one is
configured, and waits there. That is how operations drives, inspects and survives these
processes, so any change you make must keep announcing the same points in the flow.

## Running it

One process per command channel:

```
$ cd /app
$ printf '%s\n' '{"op":"snapshot"}' | python3 worker.py
{"ok": true, "version": null}
```

Configuration is the environment: `BUCKET` (default `logstore-dev`), `LOG_TABLE` (default
`log_coord`), `TABLE_PATH` (default `tables/demo`), plus the usual endpoint, region and keys. A
longer session:

```
$ printf '%s\n' '{"op":"commit","version":0,"actions":"a"}' \
                '{"op":"snapshot"}' \
                '{"op":"list_from","start":0}' | python3 worker.py
{"status": "committed", "version": 0}
{"ok": true, "version": 0}
{"ok": true, "entries": [[0, "a"]]}
```

Run two workers against the same `TABLE_PATH` in two terminals to exercise contended versions.
The reclaimer that removes completed entries past their expiry belongs to the deployment, not to
the worker.

Fix the store under `/app` so the behaviour above holds. The evidence available to you is what
the services hold — the object inventory, the coordination table — and the writers' replies.
