#!/usr/bin/env python3
"""Ledger writer: publishes settlement batches to the ledger topic, one Kafka transaction per batch.

    python3 ledger_writer.py --bootstrap HOST:PORT --topic TOPIC --txn-id TXN_ID --instance NAME --batches FILE

FILE holds one settlement batch per line:

    {"batch": "STL-20261021-0417", "entries": [{"entry": "STL-20261021-0417-1", "account": "ACC-1029", "amount_cents": 1250}, ...]}

Each batch is written as one transaction: one record per entry, key = entry id, value = the entry as
JSON plus the writer instance and the attempt number. On start the writer reads the committed ledger
and skips batches that are already there, so a standby that takes over resumes where the ledger ends.

Exit status: 0 all batches committed, 3 fenced (another instance owns the transactional id),
1 any other failure, 2 bad arguments. See OPERATIONS.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from txnclient import (AbortableTransactionError, FatalProducerError, ProducerFencedError,
                       TransactionalProducer)
from txnclient.committed import read_committed

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_FENCED = 3

# Platform settings for ledger producers (OPERATIONS.md, "Producer settings").
TRANSACTION_TIMEOUT_MS = 60000
DELIVERY_TIMEOUT_MS = 5000
MAX_ATTEMPTS_PER_BATCH = 5
MAX_PRODUCER_REBUILDS = 3
PARTITION = 0

log = logging.getLogger("ledger_writer")


def load_batches(path: str) -> list[dict]:
    batches = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                batches.append(json.loads(line))
    return batches


def committed_batch_ids(bootstrap: str, topic: str, instance: str) -> set[str]:
    ids = set()
    for record in read_committed(bootstrap, topic, PARTITION, client_id=instance):
        if record.value:
            ids.add(json.loads(record.value)["batch"])
    return ids


def entry_value(batch: dict, entry: dict, instance: str, attempt: int) -> bytes:
    return json.dumps({"batch": batch["batch"], **entry, "writer": instance, "attempt": attempt},
                      sort_keys=True).encode()


def write_batch(producer: TransactionalProducer, topic: str, batch: dict, instance: str) -> None:
    attempt = 0
    while True:
        attempt += 1
        try:
            producer.begin_transaction()
            for entry in batch["entries"]:
                producer.send(topic, PARTITION, entry["entry"].encode(), entry_value(batch, entry, instance, attempt))
            producer.commit_transaction()
            log.info("batch %s committed (attempt %d, producer %d epoch %d)", batch["batch"], attempt,
                     producer.producer_id, producer.producer_epoch)
            return
        except AbortableTransactionError as exc:
            log.warning("batch %s attempt %d failed: %s; aborting", batch["batch"], attempt, exc)
            producer.abort_transaction()
            if attempt >= MAX_ATTEMPTS_PER_BATCH:
                raise FatalProducerError(f"batch {batch['batch']}: giving up after {attempt} attempts") from exc


def run(args: argparse.Namespace) -> int:
    batches = load_batches(args.batches)
    rebuilds = 0
    while True:
        producer = TransactionalProducer(args.bootstrap, args.txn_id, args.instance,
                                         transaction_timeout_ms=TRANSACTION_TIMEOUT_MS,
                                         delivery_timeout_ms=DELIVERY_TIMEOUT_MS)
        try:
            producer.init_transactions()
            done = committed_batch_ids(args.bootstrap, args.topic, args.instance)
            pending = [b for b in batches if b["batch"] not in done]
            log.info("%d of %d batches already in the ledger; %d to write", len(batches) - len(pending),
                     len(batches), len(pending))
            for batch in pending:
                write_batch(producer, args.topic, batch, args.instance)
            log.info("all batches committed")
            return EXIT_OK
        except ProducerFencedError as exc:
            log.error("fenced: %s; another instance owns %s, exiting", exc, args.txn_id)
            return EXIT_FENCED
        except FatalProducerError as exc:
            rebuilds += 1
            if rebuilds > MAX_PRODUCER_REBUILDS:
                log.error("fatal: %s; giving up after %d producer rebuilds", exc, rebuilds - 1)
                return EXIT_FAILED
            log.error("fatal producer error: %s; rebuilding the producer (%d/%d)", exc, rebuilds,
                      MAX_PRODUCER_REBUILDS)
        finally:
            producer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bootstrap", required=True, help="HOST:PORT[,HOST:PORT...] of the MSK cluster")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--txn-id", required=True, help="transactional id shared by the active and standby writers")
    parser.add_argument("--instance", required=True, help="this instance's name (client id, and 'writer' in records)")
    parser.add_argument("--batches", required=True, help="JSONL file of settlement batches")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format=f"%(asctime)s {args.instance} %(name)s %(levelname)s %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
