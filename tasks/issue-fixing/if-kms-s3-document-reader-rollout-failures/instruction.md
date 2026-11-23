The document-reader rollout is blocked. Some documents that the old deployment can
open fail in the new deployment with KMS errors, and running the reconciliation
service has not made the rollout reliable. We still have old jobs editing and
rotating these documents; stopping those jobs early is not an option.

Repair the programs in `/app` so new readers work throughout the rollout and the
stored documents reach the new format without breaking existing old readers or
losing writes. Operations will publish the retirement signal when the last old
process has exited. `/app/OPERATIONS.md` has the service contract and normal commands;
the legacy client and document format are also there.

Keep the `migrator.py` and `newsops.py` command lines and the starter's pause
announcements available to our process supervisor. Roles run in separate fresh
working directories with `PYTHONPATH=/app`. Local files cannot coordinate them or
carry recovery state across a restart; shared durable state belongs in AWS services.
