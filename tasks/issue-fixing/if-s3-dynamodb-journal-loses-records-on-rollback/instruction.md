# Incident 4712 — journal drift after Tuesday's rollback

The object journal keeps each record as one whole object in S3; a DynamoDB head names the
current object for each record, and the fleet of three slots serves reads and updates against
those objects. Records used to be stored in the XML envelope (`format_version` 1). We are moving
them to the JSON envelope (`format_version` 2) with the usual two-step rollout, because slots run
side by side while a roll is in progress:

- build `v1` — reads and writes XML. The released baseline.
- build `v2` — reads both envelopes, keeps writing XML. The prepared build.
- build `v3` — reads both envelopes, writes JSON, and carries the new `set_attr` operation that
  attaches a named attribute to a record. The activated build.

Rolling out means asking the coordinator for `prepare`, replacing every slot with `v2`, then
asking for `activate` and replacing slots with `v3` one at a time. Rolling back an activation
means asking for `rollback` and reinstalling `v2`; there is no supported path back to `v1` once
JSON objects exist. A separate backfill process rewrites records still in XML into JSON while
the fleet keeps serving.

## What we saw

Last Tuesday we ran the planned rollout. During the activate window the on-call followed the
runbook and rolled back; later the same day the re-roll completed and the conversion report
showed every record in the new envelope. Since then:

1. The recon job that replays the operation ledger against live storage reports drift on
   records that were live during the rollback window: counters behind what the ledger
   acknowledges, and the annotator's fields (written by `v3`'s `set_attr`) missing from exactly
   those records.
2. A spot check of converted objects found attributes from the original load missing as well —
   the seed attributes no current build interprets, but which the archive policy requires us to
   keep.
3. Nothing failed at the time. Every request returned success throughout the rollout, the
   rollback and the re-roll, and no slot reported an error at any point.

A straight rollout and conversion on a fresh, quiet journal reports clean.

## What must hold afterwards

The journal keeps being a journal. Whatever mix of builds is running, and whatever a roll, a
rollback or the backfill is doing to the same records at the same time, the state an
acknowledged request produced is the state the stored record still has, and a reader on any
version in service can still read what the fleet returns. A rollout step the fleet cannot
safely take does not happen. The journal does not stop serving so that anything can converge;
the migration still reaches every record, touched or not; and rolling back an activation lands
on the prepared build.

## Preserve

- The service contract: the endpoints, request and response shapes, the three build profiles
  and their launchers, and the operation kinds each build accepts.
- The storage layout: whole-record objects in S3, one authoritative head per record, operation
  receipts and the fleet-wide control item in DynamoDB, and both envelope formats.
- The backfill's snapshot announcements on its control socket — external tooling relies on
  those pauses — and the slot and coordinator HTTP surfaces.
- Coordinated state lives in S3 and DynamoDB only. Processes are started in fresh working
  directories and must not rely on local files to coordinate.

## Running it locally

Everything runs against the local AWS endpoint with the fixed test credentials; nothing outside
the container is reachable.

```bash
# a fresh journal: bucket, table, control item, 8 records
python3 /app/seed.py --bucket journal-dev --table journal-dev

# the coordinator, the backfill, and three slots (here still the released build)
python3 /app/deployer.py --bucket journal-dev --table journal-dev --port 9101 &
python3 /app/sweeper.py --bucket journal-dev --table journal-dev --port 9120 &
python3 /app/service_v1.py --slot s0 --generation 1 --bucket journal-dev --table journal-dev --port 9110 &
python3 /app/service_v1.py --slot s1 --generation 1 --bucket journal-dev --table journal-dev --port 9111 &
python3 /app/service_v1.py --slot s2 --generation 1 --bucket journal-dev --table journal-dev --port 9112 &

# a read, an update, and a decode probe
curl -s localhost:9110/read -X POST -H 'content-type: application/json' -d '{"record_id":"rec-00"}'
curl -s localhost:9110/mutate -X POST -H 'content-type: application/json' \
  -d '{"record_id":"rec-00","op_id":"op-1","kind":"increment","arguments":{"delta":1}}'
curl -s localhost:9110/decode -X POST -H 'content-type: application/json' \
  -d '{"body":"<record format_version=\"1\"><counter>3</counter></record>","format_version":1}'

# the same probe with the new envelope, still on the released build
curl -s localhost:9110/decode -X POST -H 'content-type: application/json' \
  -d '{"body":"{\"format_version\": 2, \"counter\": 3, \"attrs\": {}}","format_version":2}'
```

Replace a slot by starting another build's launcher on the same `--slot` with a new
`--generation` (kill the old process first), then ask the coordinator to move:

```bash
curl -s localhost:9101/phase -X POST -H 'content-type: application/json' -d '{"action":"prepare"}'
curl -s localhost:9101/phase -X POST -H 'content-type: application/json' -d '{"action":"activate"}'
curl -s localhost:9101/phase -X POST -H 'content-type: application/json' -d '{"action":"rollback"}'
```

The backfill converts a list of records on request; with a `--control <socket>` configured it
announces each snapshot it has taken and waits for the controller's reply before publishing:

```bash
curl -s localhost:9120/sweep -X POST -H 'content-type: application/json' \
  -d '{"record_ids":["rec-00","rec-01"]}'
```

The stored objects are the source of truth: inspect them and the table directly when you need
to know what a record actually contains.
