# lease-balanced-stream-bridge — component reference

Field reference for the two components in scope, rendered from the reference
program's own configuration specs (`program list --format json-full inputs outputs`
prints the complete machine-readable form, and `program create aws_kinesis/mapping/file`
prints a skeleton config with every default). Duration fields take Go duration strings
such as `500ms`, `1s`, `2m`.

## Input `aws_kinesis`

Consumes records from one or more Kinesis data streams, either by automatically
balancing a stream's shards across all running instances that share the same DynamoDB
table, or by consuming explicitly listed shards. The latest consumed sequence of every
shard is stored in the DynamoDB table so that consumption resumes from it after a
restart; a sequence is only stored once the corresponding messages have been
acknowledged by the output (at-least-once delivery). The same table coordinates
balanced instances.

* **Ordering** — messages of one shard may be processed in parallel up to
  `checkpoint_limit` in-flight messages; `checkpoint_limit: 1` processes a shard's
  messages in lock-step.
* **Table schema** — with `dynamodb.create: true` the table is created if missing.
  A pre-created table must have a string HASH key `StreamID` and a string RANGE key
  `ShardID`.
* **Batching** — each shard is batched separately.
* **Stream syntax** — an element of `streams` is a stream name or ARN; several may be
  comma separated in one element; `name:shardId` selects one explicit shard (repeat the
  stream for several shards, e.g. `foo:shardId-000000000000,foo:shardId-000000000001`).
  Balanced and explicit-shard entries cannot be mixed in one input.

Each emitted message carries the metadata keys `kinesis_stream`, `kinesis_shard`,
`kinesis_partition_key` and `kinesis_sequence_number` (readable with `meta("…")` in a
`mapping` processor or interpolation).

| Field | Type | Default | Description |
|---|---|---|---|
| `streams` | string[] | — | One or more Kinesis data streams to consume from. Streams can either be specified by their name or full ARN. Shards of a stream are automatically balanced across consumers by coordinating through the provided DynamoDB table. Multiple comma separated streams can be listed in a single element. Shards are automatically distributed across consumers of a stream by coordinating through the provided DynamoDB table. Alternatively, it's possible to specify an explicit shard to consume from with a colon after the stream name. For example `foo:0` would consume the shard `0` of the stream `foo`. |
| `dynamodb` | object | — | Determines the table used for storing and accessing the latest consumed sequence for shards, and for coordinating balanced consumers of streams. |
| `dynamodb.table` | string | `""` | The name of the table to access. |
| `dynamodb.create` | bool | `false` | Whether, if the table does not exist, it should be created. |
| `dynamodb.billing_mode` | string | `"PAY_PER_REQUEST"` | When creating the table determines the billing mode. |
| `dynamodb.read_capacity_units` | int | `0` | Set the provisioned read capacity when creating the table with a `billing_mode` of `PROVISIONED`. |
| `dynamodb.write_capacity_units` | int | `0` | Set the provisioned write capacity when creating the table with a `billing_mode` of `PROVISIONED`. |
| `dynamodb.region` | string | — | The AWS region in which your resources are hosted. |
| `dynamodb.endpoint` | string | — | A custom endpoint URL for AWS API requests. Use this to connect to AWS-compatible services or local testing environments instead of the standard AWS endpoints. |
| `dynamodb.tcp` | object | — | Low-level TCP socket settings (connect timeout, keep-alive); not needed against the task venue. |
| `dynamodb.credentials` | object | — | Manually configure the AWS credentials to use (optional). For more information, see the Amazon Web Services guide. |
| `dynamodb.credentials.id` | string | — | The ID of the AWS credentials to use. |
| `dynamodb.credentials.secret` | string | — | The secret for the AWS credentials in use. |
| `dynamodb.credentials.token` | string | — | The token for the AWS credentials in use. Required only when using short-term credentials. |
| `checkpoint_limit` | int | `1024` | The maximum gap between the in flight sequence versus the latest acknowledged sequence at a given time. Increasing this limit enables parallel processing and batching at the output level to work on individual shards. Any given sequence will not be committed unless all messages under that offset are delivered in order to preserve at least once delivery guarantees. |
| `poll_period` | string | `"0s"` | An optional minimum period between GetRecords calls made against each shard. Kinesis allows a shared budget of 5 GetRecords calls per second per shard across all consumers of a stream, so a value such as `250ms` bounds this consumer to roughly four reads per second per shard, leaving headroom for other consumers of the same stream. The default of `0s` polls as fast as records are consumed. This setting has no effect when `enhanced_fan_out` is enabled. A shard is polled at most once per period, so the committed sequence advances no faster than that; values above `lease_period` are rejected. |
| `enhanced_fan_out` | object | — | Out of scope for this task. |
| `auto_replay_nacks` | bool | `true` | Whether to automatically replay rejected messages (negative acknowledgements, or nacks) at the output level. If the cause of rejections persists, leaving this option enabled can result in back pressure. Set `auto_replay_nacks` to `false` to delete rejected messages. Disabling auto replays can greatly improve memory efficiency of high throughput streams, as the original shape of the data is discarded immediately upon consumption and mutation. |
| `commit_period` | string | `"5s"` | The period of time between each update to the checkpoint table. |
| `steal_grace_period` | string | `"2s"` | Determines how long beyond the next commit period a client will wait when stealing a shard for the current owner to store a checkpoint. A longer value increases the time taken to balance shards but reduces the likelihood of processing duplicate messages. |
| `rebalance_period` | string | `"30s"` | The period of time between each attempt to rebalance shards across clients. |
| `lease_period` | string | `"30s"` | The period of time after which a client that has failed to update a shard checkpoint is assumed to be inactive. |
| `start_from_oldest` | bool | `true` | Whether to consume from the oldest message when a sequence does not yet exist for the stream. |
| `region` | string | — | The AWS region in which your resources are hosted. |
| `endpoint` | string | — | A custom endpoint URL for AWS API requests. Use this to connect to AWS-compatible services or local testing environments instead of the standard AWS endpoints. |
| `tcp` | object | — | Low-level TCP socket settings (connect timeout, keep-alive); not needed against the task venue. |
| `credentials` | object | — | Manually configure the AWS credentials to use (optional). For more information, see the Amazon Web Services guide. |
| `credentials.id` | string | — | The ID of the AWS credentials to use. |
| `credentials.secret` | string | — | The secret for the AWS credentials in use. |
| `credentials.token` | string | — | The token for the AWS credentials in use. Required only when using short-term credentials. |
| `batching` | object | — | Configure a batching policy. |
| `batching.count` | int | `0` | The number of messages at which the batch is flushed. Set to `0` to disable count-based batching. |
| `batching.byte_size` | int | `0` | The maximum total size (in bytes) that a batch can reach before it is flushed. When the combined size of all messages in the batch reaches or exceeds this limit, the batch is immediately sent to the next stage (such as a processor or output). Set to `0` to disable size-based batching. When disabled, messages are flushed based on other conditions (such as `count` or `period`). |
| `batching.period` | string | `""` | The length of time after which an incomplete batch is flushed regardless of its size. This field accepts Go duration format strings such as `100ms`, `1s`, or `5s`. Supported time units are `ns`, `us`, `ms`, `s`, `m`, and `h`. |
| `batching.check` | string | `""` | A Bloblang query that returns a boolean value indicating whether a message should end a batch. |
| `batching.processors` | processor[] | — | A list of processors to apply to a batch as it is flushed. This allows you to aggregate and archive the batch however you see fit. All resulting messages are flushed as a single batch, so splitting the batch into smaller batches with these processors has no effect. |

## Output `aws_kinesis`

Sends each message as one Kinesis record (via PutRecords) to `stream`. `partition_key`
(required) and `hash_key` (optional, sent as the explicit hash key when non-empty) are
interpolated per message, e.g. `partition_key: 'user-${! json("key") }'` or
`'${! meta("kinesis_partition_key") }'`. Records larger than 1 MiB are rejected.
Batches may be formed at the input or output level; `max_in_flight` bounds parallel
batches.

| Field | Type | Default | Description |
|---|---|---|---|
| `stream` | string | — | The stream to publish messages to. Streams can either be specified by their name or full ARN. |
| `partition_key` | string | — | A required key for partitioning messages. |
| `hash_key` | string | — | A optional hash key for partitioning messages. |
| `max_in_flight` | int | `64` | The maximum number of parallel message batches to have in flight at any given time. |
| `batching` | object | — | Configure a batching policy. |
| `batching.count` | int | `0` | The number of messages at which the batch is flushed. Set to `0` to disable count-based batching. |
| `batching.byte_size` | int | `0` | The maximum total size (in bytes) that a batch can reach before it is flushed. When the combined size of all messages in the batch reaches or exceeds this limit, the batch is immediately sent to the next stage (such as a processor or output). Set to `0` to disable size-based batching. When disabled, messages are flushed based on other conditions (such as `count` or `period`). |
| `batching.period` | string | `""` | The length of time after which an incomplete batch is flushed regardless of its size. This field accepts Go duration format strings such as `100ms`, `1s`, or `5s`. Supported time units are `ns`, `us`, `ms`, `s`, `m`, and `h`. |
| `batching.check` | string | `""` | A Bloblang query that returns a boolean value indicating whether a message should end a batch. |
| `batching.processors` | processor[] | — | A list of processors to apply to a batch as it is flushed. This allows you to aggregate and archive the batch however you see fit. All resulting messages are flushed as a single batch, so splitting the batch into smaller batches with these processors has no effect. |
| `region` | string | — | The AWS region in which your resources are hosted. |
| `endpoint` | string | — | A custom endpoint URL for AWS API requests. Use this to connect to AWS-compatible services or local testing environments instead of the standard AWS endpoints. |
| `tcp` | object | — | Low-level TCP socket settings (connect timeout, keep-alive); not needed against the task venue. |
| `credentials` | object | — | Manually configure the AWS credentials to use (optional). For more information, see the Amazon Web Services guide. |
| `credentials.id` | string | — | The ID of the AWS credentials to use. |
| `credentials.secret` | string | — | The secret for the AWS credentials in use. |
| `credentials.token` | string | — | The token for the AWS credentials in use. Required only when using short-term credentials. |
| `max_retries` | int | `0` | The maximum number of retries before giving up on the request. If set to zero there is no discrete limit. |
| `backoff` | object | — | The exponential backoff between retry attempts. The wait starts at `initial_interval` and grows with each failed attempt, up to `max_interval`. |
| `backoff.initial_interval` | string | `"1s"` | The initial period to wait between retry attempts. The retry interval increases for each failed attempt, up to the `backoff.max_interval` value. This field accepts Go duration format strings such as `100ms`, `1s`, or `5s`. |
| `backoff.max_interval` | string | `"5s"` | The maximum period to wait between retry attempts. |
| `backoff.max_elapsed_time` | string | `"30s"` | The maximum period to wait before retry attempts are abandoned. If zero then no limit is used. |
