Finance reports that commitment purchase history no longer agrees with the cloud account after interrupted approvals. Some orders sit indefinitely in progress; others turn into failures that require an operator even though the gateway could resolve them. Restarting the scheduler has not repaired the history.

Fix the purchase service in `/app`. Abandoned work must settle, and recommendations that the gateway can reconcile safely should finish automatically with the actual commitment references. A purchase is a financial obligation: recovery must never buy a recommendation twice. Keep ordinary approvals and mixed-result reporting working.

The service and deployment notes are in `/app/README.md`; the gateway contract is in `/app/gateway-api.md`. Use `python /app/worker.py approve --execution-id ID --actor EMAIL` and `python /app/worker.py sweep --once`. Preserve these entry points, their environment configuration, and the gateway request interface. Workers run synchronously in separate processes and fresh working directories; only AWS service state survives a worker loss or is shared between workers. Any pause announcements already made by the program must remain available.

Finance also reports that clicking Retry after a mixed outcome can buy successful
entries again. Keep the operator Retry and approval workflow working across
process loss and repeated requests; see README.md for its deployed contract.
