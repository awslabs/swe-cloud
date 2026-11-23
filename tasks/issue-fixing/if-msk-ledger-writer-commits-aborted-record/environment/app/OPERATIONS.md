# Operating the ledger writer

## Deployment

- Two instances, `ledger-writer-a` (active) and `ledger-writer-b` (standby), on different hosts. Both
  run `ledger_writer.py` with the same `--txn-id ledger-writer`, the same topic and the same batch
  feed. Each instance is its own process on its own host; nothing on local disk is shared.
- The supervisor starts the standby when the active misses heartbeats for 3 s or its host is being
  drained. The transactional id is the only coordination between the two: no lock, no shared state,
  and the processes do not talk to each other. The standby can therefore start at any moment,
  including while the active is in the middle of a transaction or recovering from an error.
- **Takeover.** The standby's `init_transactions()` makes it the owner of the transactional id: the
  coordinator aborts whatever transaction the previous owner had open and fences the previous
  owner's epoch. The standby then reads the committed ledger and resumes at the first batch that is
  not in it.
- The replaced instance learns that it was fenced the next time it talks to the coordinator or to
  the partition, and must then exit with status **3**. Until it does, both instances are talking to
  the broker. The supervisor neither restarts nor pages for an instance that exited 3, so an instance
  that still owns the transactional id must never exit 3: nothing would feed the ledger. Exit 1 is a
  crash: it pages on-call and the supervisor restarts the instance.
- **Once replaced, an instance never takes the transactional id back.** By then the supervisor has
  moved alerting and the batch feed to the replacement. A stale instance that registers again fences
  the live writer, which exits 3 and is not restarted, and leaves the ledger fed from a host that
  is being drained or is cut off.

## Producer settings

| setting | value | notes |
|---|---|---|
| `transaction.timeout.ms` | 60000 | Fixed by the platform team's MSK configuration for ledger producers. The writer aborts its own failed transactions; the broker's timeout is a backstop, not a recovery mechanism. |
| delivery timeout | 5000 ms | How long `send()` waits for the acknowledgement of one record. When it expires the writer does not know whether the broker has appended the record, or will. |
| attempts per batch | 5 | A failed attempt is aborted and the whole batch is retried. |
| compression | none | The audit tap reads ledger record batches raw and does not decompress. |
| producer rebuilds | 3 | After a fatal producer error the writer builds a new producer and resumes from the committed ledger. |

A delivery timeout must not take the writer down: it aborts the attempt and retries the batch in the
same process. The ledger's freshness SLO needs the writer committing again within **15 s** of a
delivery timeout.

The writer's MSK principal may use the producer and consumer APIs, with its own transactional id
only. Other transactional ids are denied, and so are the admin APIs: topic and configuration
administration, and transaction administration and inspection (WriteTxnMarkers, DescribeProducers,
DescribeTransactions, ListTransactions).

## What the ledger guarantees

- A `read_committed` consumer sees every entry of every batch exactly once, and nothing from an
  aborted attempt. Entry ids are unique: the GL export keys on them and rejects a file with a
  duplicate.
- The partition's last stable offset keeps up with its high watermark. `read_committed` consumers
  stop at the first open transaction, so a transaction left open stalls every downstream reader.

## History

- `incident/2026-09-03-failover.log`: a planned failover (host patching). This is how a takeover is
  supposed to look.
- `incident/2026-10-21/`: network incident between the writer hosts and the broker; reconciliation
  found a duplicated entry afterwards. Open.
