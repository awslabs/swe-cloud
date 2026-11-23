# flag-evaluator: interface

flag-evaluator is a feature-flag and experimentation HTTP server. Flags, their
variants, segments, constraints and distributions are stored in PostgreSQL (an RDS
instance in the AWS venue). Clients evaluate flags against an entity context over
REST, and, when enabled, every evaluation of a flag that has `dataRecordsEnabled`
is emitted as a data record onto an Amazon Kinesis stream.

The complete request/response schema of the admitted REST API is in `api.yaml`
(Swagger 2.0) next to this guide. The Kinesis record encoding is described in
`kinesis-records.md`.

## Invocation

```
program [OPTIONS]
```

The program is a long-running server; it takes no subcommand. It reads all
application configuration from environment variables at start-up (there is no
config file) and then serves HTTP until it receives SIGTERM/SIGINT.

Listener options (each also readable from the environment variable shown):

| Option | Env | Default | Meaning |
|---|---|---|---|
| `--host=` | `HOST` | `localhost` | IP to listen on |
| `--port=` | `PORT` | `18000` | TCP port for plain HTTP |
| `--help` | | | print all options and exit 0 |

Other `--help` options (timeouts, TLS listener, socket path) are generic listener
settings and are not part of the admitted scope. The server logs to stdout/stderr;
log text is not part of the interface.

All API paths are below `/api/v1`. Request and response bodies are JSON
(`Content-Type: application/json`). `GET /api/v1/health` returns
`{"status":"OK", ...}` once the server is ready to accept requests.

## Configuration

### Storage

| Variable | Use |
|---|---|
| `FLAGR_DB_DBDRIVER` | `postgres` |
| `FLAGR_DB_DBCONNECTIONSTR` | a libpq URL, e.g. `postgres://USER:PASS@HOST:PORT/DBNAME?sslmode=disable` |
| `FLAGR_DB_DBCONNECTION_DEBUG` | `true` (default) logs SQL statements; `false` silences them |

On start-up the server connects (retrying briefly) and creates or migrates its own
tables in the named database. Point each independent server instance at its own
database; a server restarted on an existing database continues with its contents.

The stored rows are observable state. These columns are read directly (other
columns may exist; their names and contents are not graded):

| Table | Graded columns |
|---|---|
| `flags` | `id`, `key`, `description`, `enabled` (boolean), `data_records_enabled` (boolean), `entity_type`, `notes`, `snapshot_id`, `deleted_at` |
| `variants` | `id`, `flag_id`, `key`, `attachment` (JSON object text), `deleted_at` |
| `segments` | `id`, `flag_id`, `description`, `rank`, `rollout_percent`, `deleted_at` |
| `constraints` | `id`, `segment_id`, `property`, `operator`, `value`, `deleted_at` |
| `distributions` | `id`, `segment_id`, `variant_id`, `variant_key`, `percent`, `deleted_at` |
| `tags` | `id`, `value` |
| `flags_tags` | `flag_id`, `tag_id` (one row per attached tag) |
| `flag_snapshots` | `id`, `flag_id` (the stored flag document column is not graded) |
| `flag_entity_types` | `key` |

`id` columns are integer primary keys; `deleted_at` is a nullable timestamp marking
a soft-deleted row (only whether it is NULL is compared). `entity_type`/`notes` are
compared as text with NULL treated as empty. Timestamps are never compared literally.

### Evaluation

| Variable | Default | Use |
|---|---|---|
| `FLAGR_EVALCACHE_REFRESHINTERVAL` | `3s` | how often evaluation state is reloaded from storage (Go duration syntax, e.g. `500ms`) |
| `FLAGR_EVAL_DEBUG_ENABLED` | `true` | global switch allowing `enableDebug` in evaluation requests |
| `FLAGR_EVAL_BATCH_SIZE` | `0` | maximum evaluations per batch request; `0` = unlimited |

Evaluation never reads the database directly: it uses an in-memory view that is
refreshed on the interval above. A change made through the management API becomes
visible to `/evaluation` only after a subsequent refresh. Observers must wait for at
least one refresh interval (or poll) after a write before asserting evaluation results.

### Data records on Kinesis

| Variable | Default | Use |
|---|---|---|
| `FLAGR_RECORDER_ENABLED` | `false` | master switch for data records |
| `FLAGR_RECORDER_TYPE` | `kafka` | set to `kinesis` |
| `FLAGR_RECORDER_KINESIS_STREAM_NAME` | `flagr-records` | target stream (must already exist) |
| `FLAGR_RECORDER_FRAME_OUTPUT_MODE` | `payload_string` | `payload_string` or `payload_raw_json` |
| `FLAGR_RECORDER_KINESIS_FLUSH_INTERVAL` | `5s` | maximum time buffered records wait before being sent |
| `FLAGR_RECORDER_KINESIS_BATCH_COUNT` | `500` | maximum records per PutRecords call (1-500) |
| `FLAGR_RECORDER_KINESIS_BATCH_SIZE` | `0` | maximum bytes per PutRecords call; `0` = service maximum |
| `FLAGR_RECORDER_KINESIS_AGGREGATE_BATCH_COUNT` | `4294967295` | maximum user records per aggregated Kinesis record |
| `FLAGR_RECORDER_KINESIS_AGGREGATE_BATCH_SIZE` | `51200` | aggregation byte threshold; `0` = default |
| `FLAGR_RECORDER_KINESIS_BACKLOG_COUNT` | `500` | in-memory backlog of pending Kinesis records |
| `FLAGR_RECORDER_KINESIS_MAX_CONNECTIONS` | `24` | concurrent PutRecords calls (1-256) |

Records are written asynchronously: an evaluation response does not wait for its
record to reach the stream. Allow at least the flush interval before reading.
Delivery failures are logged, not reported to API clients.

The grouping of Kinesis records into `PutRecords` calls (`BATCH_COUNT`, `BATCH_SIZE`),
the backlog and the connection count must be accepted but are not observable through
`GetRecords` and are not graded; the frames, how they are packed into Kinesis records
(`AGGREGATE_*`) and each record's partition key are.

### AWS endpoint

The Kinesis client uses the standard AWS SDK environment: `AWS_ENDPOINT_URL`
(here `http://engines:4566`), `AWS_REGION` (or `AWS_DEFAULT_REGION`), and
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`. These are already set in the venue.
Make sure `AWS_REGION` is exported to the server process.

The PostgreSQL database is provisioned through the RDS API, e.g. with boto3:
`create_db_instance(Engine="postgres", MasterUsername=..., MasterUserPassword=...)`,
wait for `db_instance_available`, then use the returned `Endpoint.Address` and
`Endpoint.Port`. A fresh instance contains the `postgres` database; create a
database per server instance (for example with psycopg, installed in the venue)
and pass its URL in `FLAGR_DB_DBCONNECTIONSTR`.

Unrelated features in the `--help`/environment surface (the web UI, Kafka, Pub/Sub
and in-process recorders, exposure logging, notifications, SQLite export,
authentication middleware, metrics exporters) are outside the admitted scope. Run
with `FLAGR_UI_ENABLED=false`.

## Admitted REST scope

See `api.yaml` for exact parameters and schemas.

- **Flags**: `POST /flags`, `GET /flags` (with query filters), `GET|PUT|DELETE /flags/{flagID}`,
  `PUT /flags/{flagID}/restore`, `POST /flags/{flagID}/duplicate`,
  `PUT /flags/{flagID}/enabled`, `GET /flags/entity_types`.
  `POST /flags` accepts the creation template `"template": "simple_boolean_flag"`
  (any other non-empty template name is rejected).
- **Variants**: `GET|POST /flags/{flagID}/variants`, `PUT|DELETE /flags/{flagID}/variants/{variantID}`.
  A variant has a `key` and an optional JSON-object `attachment`.
- **Segments**: `GET|POST /flags/{flagID}/segments`, `PUT /flags/{flagID}/segments/reorder`,
  `PUT|DELETE /flags/{flagID}/segments/{segmentID}`. A segment has a description, a
  `rolloutPercent` (0-100) and a rank.
- **Constraints**: `GET|POST /flags/{flagID}/segments/{segmentID}/constraints`,
  `PUT|DELETE .../constraints/{constraintID}`. A constraint is
  `{property, operator, value}` with operator one of `EQ NEQ LT LTE GT GTE EREG NEREG
  IN NOTIN CONTAINS NOTCONTAINS`. `value` is a literal in expression syntax: strings
  are double-quoted (`"\"CA\""`), numbers are bare (`18`), lists for `IN`/`NOTIN`
  are bracketed (`["CA","NY"]`). `property` names a key of `entityContext`; dotted
  paths (`user.name`) and indexes (`roles[0]`) reach into nested objects and arrays.
  A segment's constraints must all hold.
- **Distributions**: `GET|PUT /flags/{flagID}/segments/{segmentID}/distributions`; the PUT
  replaces the segment's whole distribution (`percent` values over the segment's variants
  must sum to 100).
- **Tags**: `GET|POST /flags/{flagID}/tags`, `DELETE /flags/{flagID}/tags/{tagID}`, `GET /tags`.
- **History**: `GET /flags/{flagID}/snapshots`, `GET /flags/snapshots/max_id`. Each stored
  snapshot captures a whole flag after a change.
- **Evaluation**: `POST /evaluation` with an `evalContext` (`entityID`, `entityType`,
  `entityContext` object, `flagID` or `flagKey`, `enableDebug`); `POST /evaluation/batch`
  with `entities`, and `flagIDs`/`flagKeys`/`flagTags` (+ `flagTagsOperator`). `GET` forms of
  both endpoints take the same document in a `json=` query parameter.
- **Health**: `GET /health`.

Error responses carry an HTTP status and a JSON `{"message": "..."}` body. The status
code is part of the interface; the exact message wording is not.

## Observation rules

- JSON responses are compared by decoded value; key order and whitespace are not
  significant. Booleans, numbers, strings, arrays and objects are distinct types;
  numbers compare by value (`1`, `1.0` and `1e0` are equal).
- An optional field whose value is empty (`null`, `""`, `[]`, `{}`) is equivalent to
  the field being absent. The optional zero/false values `flagID`, `segmentID`,
  `variantID`, `flagSnapshotID`, `dataRecordsEnabled` and `enableDebug` of evaluation
  results and `maxID` are likewise equivalent to absence. Required fields (for
  example a flag's `enabled`) must be present.
- Timestamps (`timestamp`, `updatedAt`, `created_at`, ...) and randomly generated
  entity IDs vary per run and are not compared literally; `timestamp` must be present
  in evaluation results.
- Debug messages (`evalDebugLog.msg` and each `segmentDebugLogs[].msg`) are
  human-oriented text; only their presence, their number and order, and the
  `segmentID` they refer to are structural.
- Error statuses are compared exactly; error bodies need only be a JSON object with
  a non-empty `message`.
- Array order in responses is significant, except that within one entity's batch
  results the flags selected through `flagTags` may come in any order among
  themselves.
- Database IDs are assigned by the server's storage in creation order within a fresh
  database.
- Kinesis records are observed through `GetRecords` on the stream. Frames are
  compared by decoded value under the rules above (a `payload_string` payload is
  parsed as JSON). The order of records belonging to different API requests is not
  significant.
