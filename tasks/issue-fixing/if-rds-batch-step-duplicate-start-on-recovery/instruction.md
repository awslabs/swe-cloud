The nightly RDS-backed batch runner has been leaving runs STARTED after node
evictions. Operators see partial extract output, a replacement worker that exits
successfully, and no downstream load. Waiting for the run timeout is currently the
only way out. Operators have also found repeated batches after database connection
resets; committed partial output must survive recovery exactly as written.

Repair the runner in `/app/solution` so these runs recover under the step retry
policy and finish successfully. Preserve the integrity guarantees in the operations
runbook: an eviction notification is not permission to execute a live step twice,
and a worker returning after recovery must not change the abandoned attempt's data
or outcome. Keep the extract → (load, audit) → publish workflow working through cold image starts and run-worker
restarts. When input validation fails a branch, operators have also seen a run
reported FAILURE while its peer continued writing; failed runs must quiesce under
the runbook shutdown contract. Operators should not have to distinguish a pod that never initialized
from one that disappeared after starting.

`/app/README.md` describes the runtime and how to start a normal run. Keep the
provided body, controller, database schema and pause announcements compatible;
implement orchestration changes in `solution/`. Processes run in separate working
directories with only PostgreSQL shared; local files are not durable coordination.
Do not signal or administer processes you did not launch, change controller state,
or use privileged database credentials from the launcher in worker code.
