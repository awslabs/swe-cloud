Since accounting delivery was parallelized, a routine scale from one member to
two has caused worker exits, replayed entries, and occasional offset jumps. Operations also saw
partitions remain behind after traffic stopped, even when both workers stayed up.
No process was killed in these incidents.

Repair the program in `/app` so graceful cooperative scaling preserves ledger
progress and healthy membership. Keep the delivery and deployment contract in
`/app/README.md`, including prompt service on partitions the member retains.
The downstream accounting operation cannot be rolled back or deduplicated.

The sandbox provides AWS APIs at `$AWS_ENDPOINT_URL` and a real MSK-backed broker;
`/app/provision.py` provisions a development topic. The README describes how to
start a worker against a broker and accounting endpoint. The verifier supplies
fresh infrastructure, an accounting endpoint, and two separately launched workers.
