# flag-evaluator: Kinesis data records

When `FLAGR_RECORDER_ENABLED=true`, `FLAGR_RECORDER_TYPE=kinesis` and an evaluated
flag has `dataRecordsEnabled: true`, the server emits data records describing
evaluation results onto the configured Kinesis stream. Each record carries a
partition key chosen by the server; the partition key returned by `GetRecords` is
observable state.

## Frame (one per evaluation result)

A frame is a UTF-8 JSON object. Its shape depends on
`FLAGR_RECORDER_FRAME_OUTPUT_MODE`:

- `payload_string` (default): `{"payload": "<JSON text>", "encrypted": false}`.
  `payload` is the evaluation result serialized as a JSON *string*.
- `payload_raw_json`: `{"payload": { ...evaluation result object... }}`.

The evaluation result inside the payload uses the same field names as the
`evalResult` schema of `POST /evaluation` in `api.yaml` (for example
`evalContext`, `flagID`, `flagKey`, `flagSnapshotID`, `segmentID`, `variantID`,
`variantKey`, `variantAttachment`, `timestamp`). Encryption of frames is not
available for the Kinesis recorder.

## Aggregated Kinesis records

Frames are batched by the producer. By default several frames are packed into a
single Kinesis record using the public **KPL aggregated record format** (the format
the Kinesis Producer Library writes and the Kinesis Client Library de-aggregates):

```
+----------------------+-----------------------------+------------------------+
| magic: F3 89 9A C2   | protobuf AggregatedRecord   | MD5 of protobuf bytes  |
| (4 bytes)            | (variable length)           | (16 bytes)             |
+----------------------+-----------------------------+------------------------+
```

The protobuf message (proto2):

```
message AggregatedRecord {
  repeated string partition_key_table     = 1;
  repeated string explicit_hash_key_table = 2;
  repeated Record records                 = 3;
}
message Tag {
  required string key   = 1;
  optional string value = 2;
}
message Record {
  required uint64 partition_key_index     = 1;
  optional uint64 explicit_hash_key_index = 2;
  required bytes  data                    = 3;
  repeated Tag    tags                    = 4;
}
```

Each `Record.data` is one frame. `partition_key_index` indexes
`partition_key_table` (zero-based). Protobuf wire encoding: every field is a varint
key `(field_number << 3) | wire_type`; wire type 0 is a base-128 little-endian
varint, wire type 2 is a varint length followed by that many bytes (strings, bytes
and embedded messages). A Kinesis record whose data does not start with the magic
bytes is a plain (non-aggregated) record whose data is a single frame.

The `FLAGR_RECORDER_KINESIS_AGGREGATE_*` and `FLAGR_RECORDER_KINESIS_BATCH_*`
settings (see `interface.md`) control how frames are grouped into Kinesis records
and PutRecords calls. A frame whose size (data plus partition key) exceeds
`FLAGR_RECORDER_KINESIS_AGGREGATE_BATCH_SIZE` bytes is sent as a plain record.

## Reading records

Use boto3 against the venue endpoint: `describe_stream` (or `list_shards`) for the
shard IDs, `get_shard_iterator(ShardIteratorType="TRIM_HORIZON")`, then repeated
`get_records` until the expected frames arrived. Records appear only after the
producer flushes (`FLAGR_RECORDER_KINESIS_FLUSH_INTERVAL`), so poll with a deadline.
