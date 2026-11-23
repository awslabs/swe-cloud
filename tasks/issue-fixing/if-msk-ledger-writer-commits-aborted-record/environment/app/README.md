# ledger-writer

Publishes settlement batches to the `ledger` topic on MSK. Every batch is one Kafka transaction, so a
`read_committed` consumer (reconciliation, the GL export, the balances service) sees either all of a
batch's entries or none of them. The writer runs as an active/standby pair sharing one transactional
id; see `OPERATIONS.md` for how the pair behaves in production.

```
ledger_writer.py          the writer (CLI, batch loop, retry of aborted batches, producer rebuilds)
txnclient/
  producer.py             transactional producer: a small synchronous port of the Java client's
                          TransactionManager/Sender (init, begin, send, commit, abort, epoch bumps)
  committed.py            read_committed view of a partition, used to resume from the ledger
  protocol.py             Kafka wire protocol codecs and the broker connection
  errors.py               error codes and the producer's exception classes
scripts/dev_cluster.py    create a development MSK cluster and topic, print the bootstrap
samples/batches.jsonl     sample input
incident/                 material from the 2026-10-21 incident and an earlier planned failover
```

The client is pure Python on the standard library. The record format and the CLI are consumed by
other teams and by the supervisor; keep both as they are.

## Running it

The environment has a local AWS endpoint (`AWS_ENDPOINT_URL`). MSK clusters there are single-broker
Redpanda clusters speaking the Kafka protocol, so they behave like the production brokers do, and
run in a shared engine host.

```bash
BOOTSTRAP=$(python3 scripts/dev_cluster.py)          # cluster "ledger-dev", topic "ledger"; reused if present
python3 ledger_writer.py --bootstrap "$BOOTSTRAP" --topic ledger --txn-id ledger-writer \
    --instance ledger-writer-a --batches samples/batches.jsonl
```

Running it again with `--instance ledger-writer-b` is a takeover: the second instance fences the
first and finds every batch already in the ledger.

The engine host only has room for a handful of MSK clusters at a time. `dev_cluster.py` reuses its
cluster by name; separate experiments with their own topics and transactional ids instead. Delete any extra
clusters you make (`boto3.client("kafka").delete_cluster(ClusterArn=...)`).

`confluent-kafka` is installed for ad-hoc inspection (for example a `read_committed` consumer);
the writer itself does not use it.
