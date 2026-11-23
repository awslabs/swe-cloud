The order-change feed occasionally has holes after worker replacement. Downstream
has no committed output for some of the missing changes. Normal shutdowns have
not reproduced it. Our rolling launcher now brings up a successor before the
previous process has necessarily finished draining.

Repair the reader in `/app` so replacements recover outstanding changes,
including changes made during downtime. Keep `latest` useful across replacements,
and preserve the existing acknowledgement, batching and legacy checkpoint behavior.
Rolling handoffs must obey the same recovery contract as an abrupt process loss.

Run it with `python3 /app/cdc_reader.py --help`; `/app/README.md` describes the
operating contract and local AWS endpoint. Each process has a fresh working and
temporary directory. Only the configured DynamoDB checkpoint partition is durable
subscription state. Keep the CLI, line protocol and launcher instrumentation
compatible, and leave source data untouched.
