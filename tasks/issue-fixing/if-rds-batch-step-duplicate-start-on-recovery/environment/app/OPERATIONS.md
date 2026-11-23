# Worker loss handling

The control plane and the database have independent failure domains. A deleted or
Terminating pod can still be executing against PostgreSQL. Kubernetes may create a
replacement as soon as termination is apparent, even with parallelism=1. A Job has
no terminal condition until all its pods terminate. An unreachable pod can remain
Terminating indefinitely; its replacement can finish without doing useful work.
Do not infer that the original body stopped from a replacement's exit code.

Our recovery objective is run SUCCESS within 60 seconds of an extract worker dying
or losing database access. A normal run, including image startup, should finish within 65 seconds. The whole
run timeout is a last resort and does not satisfy recovery. Health polling is every
two seconds. A five-second loss of worker contact is the operational failure window;
workers should report contact about once a second while doing work, including
while a batch transaction is still open. Slow batch SQL is normal and is not a
reason to spend an application retry. Database time
is authoritative; slow individual polls are expected. A pod status change alone
must not spend extract's one retry when its original still has database contact.

Isolation can suspend a process inside a database write, disconnect all its sessions
and prevent new logins. A frozen node can also suspend a process while its sessions
stay connected, leaving its batch transaction open until it returns. A suspended
process can return much later with credentials working again. Reconnecting is expected, not evidence of a new execution attempt.
Do not rely on a session lock surviving connection loss. Do not terminate the old
process to resolve uncertainty: that belongs to the controller and may be impossible
on the unreachable node.

A replacement must never enter a body that was already started for its Job. Once
an attempt is abandoned for retry, none of its remaining batches, heartbeats or
terminal events may restore its authority or land after its STEP_UP_FOR_RETRY.
Partial batches already committed remain historical data; the next attempt writes
its own complete set. Do not erase those rows or deduplicate across attempts.
A successful run must complete every step in `harness/workflow.py` once, in
dependency order. No branch starts before authoritative extract success and no
publish starts until both branches succeed. Retry exhaustion must close the run
with FAILURE rather than hang or invent success.

Connection loss also happens without a node eviction. In that case a worker may
still have fresh contact through its other sessions. Reopen the failed connection
and resolve the batch result without restarting the body or spending an application
retry. A rolled-back transaction, a committed transaction whose response
was lost, and a still-resolving commit are all possible. Deferred database checks
continue inside COMMIT after the client has lost its connection. Partial history
must neither disappear nor gain duplicates.
An old worker that later loses authority must still stop, even if it was resolving
one of these uncertain transactions. The same attempt identity governs admission,
contact, individual batch replays and the closing outcome.

Job submission can succeed without any worker ever reaching its entrypoint. Allow
ten seconds from the durable Job submission for initial worker admission; a pod
still initializing during that allowance is not a lost running worker. After that,
a Job with no admitted worker must recover under the same application retry budget
as a lost running worker, even if no STEP_START exists. Record its retry or failure
closure without inventing a start or invoking its body. The recovery objective of
60 seconds applies from the initial submission in this case. A late original or
replacement from a closed Job must not enter the body, including after the run ends.

Run-worker restarts do not reset Job age, contact age, retry budget, or completed
work. Supervisors can overlap and restart repeatedly while pods remain alive. The
same recovery objectives apply; restarting a monitor is not a reason to spend a
step retry. Terminal run outcomes are final. Once the retry budget is exhausted,
close FAILURE within 15 seconds of the final worker loss (or, for an unstarted
Job, within 15 seconds of the end of its admission allowance). These deadlines
include the normal two-second health polling interval.

A permanent branch failure changes the run's objective to safe shutdown. Stop
submitting work, cancel every submitted unfinished peer, and close the run FAILURE
within 25 seconds of the failing STEP_FAILURE. A peer that already succeeded stays
successful; never relabel completed work. A submitted peer with no admitted body
still needs a cancellation closure, and its late pods must refuse admission.
This is fail-fast: a suspended peer must not be left authorized until it returns.
Do not wait for Kubernetes termination or worker contact expiry to cancel a peer.

For each cancelled peer, preserve committed partial history and prohibit batches,
heartbeats and terminal outcomes after STEP_CANCELLED. Unresolved transactions
can still commit, so cancel cannot assume a lost client means its writes stopped.
The run may become terminal only after all submitted attempts are closed; no
admitted transaction may add history after the terminal run timestamp. Original
processes can reconnect after shutdown and monitors can restart during draining.
Application exceptions in a body must produce the normal retry/failure outcome,
with the same admission, transaction and closing guarantees as worker-loss recovery.
