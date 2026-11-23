"""Durable admission, independently renewable contact, and transactional fences."""
from contextlib import contextmanager
import psycopg
from .storage import connect, event, events, launch


class Revoked(Exception):
    pass


def initialize(conn):
    conn.execute('SELECT pg_advisory_xact_lock(81278031)')
    conn.execute('''CREATE TABLE IF NOT EXISTS solver.attempts(
        run_id text, step_key text, current_attempt int NOT NULL, owner text,
        PRIMARY KEY(run_id,step_key))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS solver.contacts(
        run_id text, step_key text, attempt int, last_contact timestamptz,
        PRIMARY KEY(run_id,step_key,attempt))''')


def schedule(conn, run, step, attempt):
    conn.execute('''INSERT INTO solver.attempts VALUES (%s,%s,%s,NULL)
        ON CONFLICT DO NOTHING''', (run,step,attempt))
    # Both the intent and its submission survive executor death together.
    launch(conn,run,step,attempt)


def claim(conn, run, step, attempt, owner):
    if conn.execute("SELECT 1 FROM dagster.event_logs WHERE run_id=%s AND dagster_event_type='STEP_FAILURE' LIMIT 1",(run,)).fetchone():
        return False
    row = conn.execute('''SELECT current_attempt,owner FROM solver.attempts
        WHERE run_id=%s AND step_key=%s''', (run,step)).fetchone()
    if row != (attempt,None):
        return False
    # Only pending claimants wait. An already-admitted replacement never queues
    # an exclusive lock behind a long batch and interrupts its owner's contact.
    row = conn.execute('''SELECT current_attempt,owner FROM solver.attempts
        WHERE run_id=%s AND step_key=%s FOR UPDATE''', (run,step)).fetchone()
    if row != (attempt,None):
        return False
    if any(a==attempt and k in ('STEP_START','STEP_RESTARTED','STEP_SUCCESS','STEP_FAILURE','STEP_UP_FOR_RETRY','STEP_CANCELLED')
           for a,k,_ in events(conn,run,step)):
        return False
    conn.execute('UPDATE solver.attempts SET owner=%s WHERE run_id=%s AND step_key=%s', (owner,run,step))
    conn.execute('''INSERT INTO solver.contacts VALUES (%s,%s,%s,clock_timestamp())''',(run,step,attempt))
    event(conn,run,step,attempt,'STEP_START' if attempt==1 else 'STEP_RESTARTED')
    return True


@contextmanager
def authorized(run, step, attempt, owner, exclusive=False):
    with connect() as conn:
        lock = 'UPDATE' if exclusive else 'SHARE'
        row = conn.execute(f'''SELECT current_attempt,owner FROM solver.attempts
            WHERE run_id=%s AND step_key=%s FOR {lock}''', (run,step)).fetchone()
        if row != (attempt,owner):
            raise Revoked()
        yield conn


def holds(conn, run, step, attempt, owner, lock=''):
    row = conn.execute(f'''SELECT current_attempt,owner FROM solver.attempts
        WHERE run_id=%s AND step_key=%s {lock}''', (run,step)).fetchone()
    if row != (attempt,owner):
        raise Revoked()


def renew(run, step, attempt, owner):
    # One autocommit statement: the fence lock lives only inside the server call,
    # so a process frozen between two client round trips never retains it.
    conn = psycopg.connect(connect_timeout=3, autocommit=True)
    try:
        renewed = conn.execute('''WITH held AS (SELECT run_id,step_key,current_attempt FROM solver.attempts
                WHERE run_id=%s AND step_key=%s AND current_attempt=%s AND owner=%s FOR SHARE)
            UPDATE solver.contacts c SET last_contact=clock_timestamp() FROM held
            WHERE c.run_id=held.run_id AND c.step_key=held.step_key AND c.attempt=held.current_attempt''',
            (run,step,attempt,owner)).rowcount
    finally:
        conn.close()
    if not renewed:
        raise Revoked()


def reconcile(conn, run, step, attempt, max_retries):
    row = conn.execute('''SELECT current_attempt,owner FROM solver.attempts
        WHERE run_id=%s AND step_key=%s FOR UPDATE NOWAIT''', (run,step)).fetchone()
    if row is None or row[0] != attempt:
        return
    kinds = [k for a,k,_ in events(conn,run,step) if a==attempt]
    if any(k in kinds for k in ('STEP_SUCCESS','STEP_FAILURE','STEP_UP_FOR_RETRY','STEP_CANCELLED')):
        return
    if row[1] is None:
        # No contact exists before admission. Use the immutable Job submission,
        # not a supervisor's launch time or the five-second running-worker window.
        stale = conn.execute('''SELECT created_at < clock_timestamp()-interval '10 seconds'
            FROM dagster.k8s_jobs WHERE run_id=%s AND step_key=%s AND attempt=%s''',
            (run,step,attempt)).fetchone()
    else:
        stale = conn.execute('''SELECT last_contact < clock_timestamp()-interval '5 seconds'
            FROM solver.contacts WHERE run_id=%s AND step_key=%s AND attempt=%s''',
            (run,step,attempt)).fetchone()
    if stale != (True,):
        return
    conn.execute('''UPDATE solver.attempts SET current_attempt=current_attempt+1,owner=NULL
        WHERE run_id=%s AND step_key=%s''', (run,step))
    event(conn,run,step,attempt,'STEP_UP_FOR_RETRY' if attempt<=max_retries else 'STEP_FAILURE','Worker unavailable')


def cancel(conn, run, step, attempt):
    # A healthy peer can have unresolved SQL. Do not queue a lock that would
    # obstruct its contact; revisit it after that transaction finishes.
    row = conn.execute('''SELECT current_attempt FROM solver.attempts
        WHERE run_id=%s AND step_key=%s FOR UPDATE NOWAIT''',(run,step)).fetchone()
    if row is None or row[0] != attempt:
        return
    kinds=[k for a,k,_ in events(conn,run,step) if a==attempt]
    if any(k in kinds for k in ('STEP_SUCCESS','STEP_FAILURE','STEP_UP_FOR_RETRY','STEP_CANCELLED')):
        return
    conn.execute('''UPDATE solver.attempts SET current_attempt=current_attempt+1,owner=NULL
        WHERE run_id=%s AND step_key=%s''',(run,step))
    event(conn,run,step,attempt,'STEP_CANCELLED','Sibling failed')
