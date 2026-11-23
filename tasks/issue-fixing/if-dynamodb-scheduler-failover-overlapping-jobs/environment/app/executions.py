"""The per-job running record and what the processes do to it.

One item per job, JOB#<job>/ACTIVE, is the running record: which attempt owns
the job right now.  A claim is minted when a nominal is admitted, taken by one
of the workers the job names, maintained by that worker for as long as the body
runs, and closed when the body reports completion.  The fields are:

  run_id, claim_token   identity of the attempt that owns the job
  worker                id of the worker that took the claim (unset until taken)
  slot                  nominal the claim was admitted for
  status                LIVE while the attempt holds the job
  heartbeat_at          time of the last write by the minter, taker or maintainer
  lease_until           heartbeat_at + run_lease_s, set by the same write
"""

from proto import CONFIG, new_id, now


def claim(st, job, slot, targets, t=None):
    """Admit a nominal: mint the attempt and write the running record."""
    t = t if t is not None else now()
    run_id = new_id()
    token = new_id()
    st.put(st.job_key(job), "ACTIVE", {
        "job": job,
        "slot": int(slot),
        "run_id": run_id,
        "claim_token": token,
        "status": "LIVE",
        "targets": list(targets),
        "lease_until": t + CONFIG["run_lease_s"],
        "heartbeat_at": t,
        "started_at": t,
    })
    st.put(st.job_key(job), st.run_sk(run_id), {
        "job": job,
        "slot": int(slot),
        "run_id": run_id,
        "claim_token": token,
        "status": "LIVE",
        "started_at": t,
    })
    return run_id, token


def take(st, job, run_id, worker, t=None):
    """A named worker takes an untaken claim.  First taker wins, once."""
    t = t if t is not None else now()
    st.update(st.job_key(job), "ACTIVE", {
        "worker": worker,
        "heartbeat_at": t,
        "lease_until": t + CONFIG["run_lease_s"],
    }, condition="attribute_not_exists(#w)", names={"#w": "worker"})
    st.update(st.job_key(job), st.run_sk(run_id), {"worker": worker})


def renew(st, job, run_id, token, worker, t=None):
    """Maintain the claim while the body runs.  Identity is compared."""
    t = t if t is not None else now()
    st.update(st.job_key(job), "ACTIVE", {
        "heartbeat_at": t,
        "lease_until": t + CONFIG["run_lease_s"],
    }, condition="#r = :r AND #c = :c AND #w = :w AND #s = :s",
       names={"#r": "run_id", "#c": "claim_token", "#w": "worker", "#s": "status"},
       values={":r": {"S": run_id}, ":c": {"S": token},
               ":w": {"S": worker}, ":s": {"S": "LIVE"}})


def complete(st, job, run_id, token, result="ok", t=None):
    """Close the running record on a completion report."""
    t = t if t is not None else now()
    st.delete(st.job_key(job), "ACTIVE")
    st.update(st.job_key(job), st.run_sk(run_id), {
        "status": "DONE",
        "finished_at": t,
        "result": result,
    })
    return True
