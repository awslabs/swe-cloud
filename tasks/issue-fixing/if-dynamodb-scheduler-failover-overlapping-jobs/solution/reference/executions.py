"""The per-job running record and what the processes do to it.

One item per job, JOB#<job>/ACTIVE, is the running record: which attempt owns
the job right now.  A claim is minted when a nominal is admitted, taken by one
of the workers the job names, maintained by that worker for as long as the body
runs, and closed when the body reports completion.

Ownership is a durable fact, not a property of whichever scheduler happens to
be deciding: the record decides, so it survives the scheduler that wrote it.
A claim is valid until the instant its lease names; a worker that is still
running keeps pushing that instant forward, so a lapsed lease means nobody is
maintaining the attempt any more and the job may be released.  Both the release
of a lapsed claim and the closing of a completed one are decided against the
credentials the record carries, so a late write belonging to a superseded
attempt cannot dislodge the attempt that owns the job now.
"""

from botocore.exceptions import ClientError

from proto import CONFIG, cond_failed, new_id, now


def blocking(st, job, t=None):
    """True while a valid attempt owns the job.

    A record whose lease has lapsed is released against its own credentials and
    stops blocking; a record that is being maintained does not.
    """
    t = t if t is not None else now()
    row = st.active(job)
    if row is None or row.get("status") != "LIVE":
        return False
    if row.get("lease_until", 0) >= t:
        return True
    retire(st, job, row, t)
    return False


def retire(st, job, row, t=None):
    """Release a lapsed claim.  Only the exact lapsed attempt is released."""
    t = t if t is not None else now()
    try:
        st.update(st.job_key(job), "ACTIVE", {
            "status": "ABANDONED",
            "finished_at": t,
        }, condition="#r = :r AND #c = :c AND #s = :s AND #l < :t",
           names={"#r": "run_id", "#c": "claim_token",
                  "#s": "status", "#l": "lease_until"},
           values={":r": {"S": row["run_id"]},
                   ":c": {"S": row["claim_token"]},
                   ":s": {"S": "LIVE"},
                   ":t": {"N": repr(t)}})
    except ClientError as err:
        if not cond_failed(err):
            raise
        return False
    st.update(st.job_key(job), st.run_sk(row["run_id"]), {
        "status": "ABANDONED",
        "finished_at": t,
    })
    return True


def claim(st, job, slot, targets, t=None):
    """Admit a nominal: mint the attempt and write the running record.

    Only a job with no valid attempt can be admitted; anything else - a live
    record, or one released and left behind - is replaced, never silently
    overwritten while it still counts.
    """
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
    }, condition="attribute_not_exists(#k) OR #st <> :live OR #l < :t",
       names={"#k": "sk", "#st": "status", "#l": "lease_until"},
       values={":live": {"S": "LIVE"}, ":t": {"N": repr(t)}})
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
    """Close the running record on a completion report.

    The report is only applicable to the attempt the record actually holds;
    anything else is refused and changes nothing.
    """
    t = t if t is not None else now()
    try:
        st.delete(st.job_key(job), "ACTIVE",
                  condition="#r = :r AND #c = :c AND #s = :s",
                  names={"#r": "run_id", "#c": "claim_token", "#s": "status"},
                  values={":r": {"S": run_id}, ":c": {"S": token},
                          ":s": {"S": "LIVE"}})
    except ClientError as err:
        if not cond_failed(err):
            raise
        return False
    st.update(st.job_key(job), st.run_sk(run_id), {
        "status": "DONE",
        "finished_at": t,
        "result": result,
    })
    return True
