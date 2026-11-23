# Delivery pipeline — worker contract

This document is the operating contract the rest of the pipeline depends on. The
supervisor, the append service, the ingestion side and the delivery counters all
talk to the worker exactly as described here, so any change to the worker has to
keep these shapes and rules in place.

## Services

Everything runs against the local AWS endpoint (`http://localhost:4566`,
`us-east-1`, static credentials `test`/`test`).

| Thing | Where |
|---|---|
| input stream | one shard, named in the config (`input_stream`, `input_shard`) |
| output stream | one shard, named in the config (`output_stream`) |
| partition state | DynamoDB table named in the config (`table`) |
| append service | HTTP, `adapter_url` |

Messages are routed to a partition by `messageId`, so the same `messageId` is
always delivered to the same partition.

## Input records

Each input record's data is UTF-8 JSON:

```json
{"kind": "DATA", "input_sequence": "7", "messageId": "…", "payload": "…"}
```

`input_sequence` is the position of the record in the partition's input, as an
increasing decimal string starting at `"1"`. Records may be delivered more than
once; the partition's checkpoint says which positions have been accounted for.

## Worker process

```
worker.py --config <config.json> --worker-id <id>
```

The config is written by the supervisor and supplies the endpoint, credentials,
stream and table names, `run_id`, `partition_id`, the assigned `assigned_epoch`,
`adapter_url`, and the numeric operating parameters (`poll_interval_ms`,
`api_timeout_seconds`, `sdk_total_attempts`, `get_records_limit`,
`hook_wait_seconds`).

stdout is the control channel and must carry nothing but the JSON lines below.
Diagnostics go to stderr. The process runs in its own working directory and
cannot rely on any local file left behind by another process.

## Control protocol

| Direction | Line | Meaning |
|---|---|---|
| worker → supervisor | `{"event": "READY", "epoch": "…", "checkpoint": "…"}` | assignment loaded, waiting for GO |
| supervisor → worker | `{"cmd": "GO"}` | start serving the partition |
| worker → supervisor | `{"event": "AFTER_SEEN_BEFORE_PUBLISH", "input_sequence": "…", "messageId": "…"}` | the partition's row for this key is durable and the publication request is about to be submitted |
| supervisor → worker | `{"cmd": "CONTINUE"}` | the pause is over; submit the request |
| worker → supervisor | `{"event": "PUBLISHED", …}` / `{"event": "SUPPRESSED", …}` | what the worker did with one input record |
| supervisor → worker | `{"cmd": "QUIESCE"}` | stop taking new input, finish what is in flight |
| worker → supervisor | `{"event": "QUIESCED"}` | nothing further will be published by this process |

The supervisor may hold `CONTINUE` for as long as it likes and may never send
it. Every publication the worker attempts is preceded by the
`AFTER_SEEN_BEFORE_PUBLISH` line for that key, and the row it announces must
already be durable in the table when the line is written.

## Partition state

Key attributes are `pk` = `RUN#<run_id>#PART#<partition_id>` and `sk`.

**Control row** — `sk` = `CONTROL`:

| Attribute | Meaning |
|---|---|
| `owner_id` | the worker id currently assigned to the partition |
| `epoch` | assignment counter; raised by one on each reassignment |
| `input_checkpoint` | `"START"`, or the last input position accounted for |
| `recovery_boundary` | `"NONE"`, or the `barrier_id` of a boundary record in the output stream, written by the append service when the partition was reassigned |

**Seen row** — `sk` = `SEEN#<messageId>`:

| Attribute | Meaning |
|---|---|
| `seen` | `1` |
| `input_sequence` | the input position the key arrived on |
| `payload` | the payload as it arrived |
| `publication_state` | `PENDING` or `CONFIRMED` |
| `version` | changes on every write to the row |
| `updated_by` | the worker id that last wrote the row |

Writes are conditional, and the conditions are part of the contract:

- creating a seen row fails if the row already exists (`attribute_not_exists`);
- changing a seen row requires the `version` it was read with;
- the supervisor reassigns the partition only when `epoch` and `owner_id` still
  name the predecessor it has confirmed gone;
- the checkpoint moves only for the assigned owner, from the position it read.

Reads of the base table are done with consistent reads.

## Append service

```
POST /publish   {"run_id","partition_id","epoch","input_sequence","messageId","payload"}
              -> 200 {"status": "appended", "sequence": "<output sequence number>"}
              -> 502 {"status": "failed", "error": "…"}     the stream did not take the record

GET  /recovery-boundary -> {"status": "boundary", "barrier_id": "…", "sequence": "…"}
                        -> {"status": "none"}

GET  /pending -> {"status": "ok", "accepted": <number of requests accepted and not yet answered>}
```

The service accepts a request, writes one output record for it, and answers with
the sequence number the stream assigned. It does not keep per-message state and
it does not answer per-message queries. A request it has accepted is answered
only once its record is in the stream, and `/pending` counts how many such
requests it is holding.

The supervisor reassigns a partition only after the previous worker's
connections to the service are closed. By then every request that worker
submitted has either been accepted by the service or was never received and
never will be: nothing from a dead worker is still on its way to the service.

## Rules the rest of the pipeline relies on

1. The partition's row for a key is durable in the table before the publication
   request for that key is submitted, and the pause is announced at that point.
2. Everything the worker publishes goes through `POST /publish`. The worker does
   not write to the output stream any other way.
3. The worker speaks the protocol above, including `READY` before `GO`,
   `QUIESCED` after `QUIESCE`, and it exits when its assignment no longer names
   it.
4. Nothing the worker starts outlives it: no separate publisher process, no
   thread that is still working after `QUIESCED`.
5. A worker may be killed without warning at any point in its life. The next
   worker assigned to the partition sees the same table, the same streams and
   the same partition id, and starts from the checkpoint the table names.
