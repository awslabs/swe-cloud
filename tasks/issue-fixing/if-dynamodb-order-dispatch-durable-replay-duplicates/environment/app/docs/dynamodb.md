# DynamoDB client notes

The emulator supports conditional item writes, consistent reads and transactions. All tables
in this deployment are in one region and account. Transactions may include a ConditionCheck
and writes to different items. An item cannot appear in more than one action in one transaction.
A failed condition cancels the transaction; CancellationReasons describes the individual actions.
A throughput or server error is different from a condition failure.

TransactWriteItems accepts an optional ClientRequestToken (maximum 36 characters). Requests
with identical tokens and parameters are idempotent for ten minutes. A successful repetition
returns the earlier outcome; conditions are not reevaluated. Changed parameters under the same
token cause IdempotentParameterMismatch. In particular, timestamps in expression values are
request parameters. The SDK can populate an omitted token for a single API call and its own
internal retries; a fresh API call is a new request. Transaction tokens and their replay state
are managed by DynamoDB, outside the application's process. These semantics are supported by
the local emulator.

A transport or 5xx error is not evidence of rollback. A consistent GetItem returns the current
item, which may include changes from other clients since the uncertain request. The absence
of a row is not proof that an earlier write was never applied. No table in this deployment
has TTL deletion enabled.

API reference: https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html

TransactWriteItems supports at most 100 actions and 4 MiB total. A transaction cannot contain
multiple actions on one item, including condition checks. BatchWriteItem allows 25 writes and
has no per-item conditions; it is not an atomic multi-item acquisition. Queries and scans may be
paginated, and an individual item is limited to 400 KiB. Larger logical operations need multiple
service calls. Consistent reads do not reserve an item against a writer that acts after the read.
