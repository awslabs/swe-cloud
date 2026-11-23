"""Leader lease: which scheduler currently owns the schedule.

CLUSTER/LEADER holds {owner, epoch, lease_until}.  A scheduler takes the lease
only when nobody holds a live one; each takeover advances the epoch by one and
never rewinds it.  The owner maintains the lease while it leads and steps down
the moment its ownership is no longer what the table says.
"""

from botocore.exceptions import ClientError

from proto import CONFIG, now

LEADER_PK = "CLUSTER"
LEADER_SK = "LEADER"


def leader(st):
    return st.get(LEADER_PK, LEADER_SK)


def try_acquire(st, me, t=None):
    """Take the lease if it is free.  Returns the new epoch, or None."""
    t = t if t is not None else now()
    cur = leader(st)
    if cur is not None and cur.get("lease_until", 0) >= t:
        return None
    epoch = (cur.get("epoch", 0) + 1) if cur else 1
    try:
        st.put(LEADER_PK, LEADER_SK, {
            "owner": me,
            "epoch": epoch,
            "lease_until": t + CONFIG["leader_lease_s"],
            "started_at": t,
        }, condition="attribute_not_exists(#l) OR #l < :t",
           names={"#l": "lease_until"},
           values={":t": {"N": repr(t)}})
    except ClientError as err:
        if err.response["Error"]["Code"] in ("ConditionalCheckFailedException",
                                             "TransactionCanceledException"):
            return None
        raise
    return epoch


def renew(st, me, epoch, t=None):
    """Extend our own lease.  False means we no longer own the schedule."""
    t = t if t is not None else now()
    try:
        st.update(LEADER_PK, LEADER_SK, {
            "lease_until": t + CONFIG["leader_lease_s"],
        }, condition="#o = :o AND #e = :e",
           names={"#o": "owner", "#e": "epoch"},
           values={":o": {"S": me}, ":e": {"N": repr(epoch)}})
    except ClientError as err:
        if err.response["Error"]["Code"] in ("ConditionalCheckFailedException",
                                             "TransactionCanceledException"):
            return False
        raise
    return True
