"""Recoverable executor: each poll reconciles durable state under a short run lock."""
import argparse
import time
import psycopg
from .storage import connect, events
from .authority import initialize, reconcile, schedule, cancel
from harness.workflow import DEPENDENCIES, RETRIES


def tick(conn, run):
    # Competing supervisors may run the same command. A stale process does not
    # get to overwrite a newer executor's terminal result or replay a closure.
    status = conn.execute('SELECT status FROM dagster.runs WHERE run_id=%s FOR UPDATE NOWAIT',
                          (run,)).fetchone()[0]
    if status in ('SUCCESS','FAILURE'):
        return True
    conn.execute("UPDATE dagster.runs SET status='STARTED',updated_at=clock_timestamp() WHERE run_id=%s", (run,))
    histories={step:events(conn,run,step) for step in DEPENDENCIES}
    if any(k=='STEP_FAILURE' for h in histories.values() for _,k,_ in h):
        # Failure is a durable draining phase, not yet the terminal run outcome.
        # Close every submitted unfinished attempt, even one that never started.
        jobs=conn.execute('SELECT step_key,attempt FROM dagster.k8s_jobs WHERE run_id=%s',(run,)).fetchall()
        drained=True
        for step,attempt in jobs:
            if any(a==attempt and k in ('STEP_SUCCESS','STEP_FAILURE','STEP_UP_FOR_RETRY','STEP_CANCELLED')
                   for a,k,_ in histories[step]):
                continue
            try:
                with conn.transaction():
                    cancel(conn,run,step,attempt)
            except psycopg.errors.LockNotAvailable:
                drained=False
        if drained:
            conn.execute("UPDATE dagster.runs SET status='FAILURE',updated_at=clock_timestamp() WHERE run_id=%s",(run,))
        return drained
    completed={step for step,h in histories.items() if any(k=='STEP_SUCCESS' for _,k,_ in h)}
    for step,deps in DEPENDENCIES.items():
        if step in completed or not all(dep in completed for dep in deps):
            continue
        history=histories[step]
        attempt=1+sum(k=='STEP_UP_FOR_RETRY' for _,k,_ in history)
        schedule(conn,run,step,attempt)
        try:
            with conn.transaction():
                reconcile(conn,run,step,attempt,RETRIES[step])
        except psycopg.errors.LockNotAvailable:
            pass
    if len(completed)!=len(DEPENDENCIES):
        return False
    conn.execute("UPDATE dagster.runs SET status='SUCCESS',updated_at=clock_timestamp() WHERE run_id=%s",(run,))
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id',required=True)
    run = parser.parse_args().run_id
    ready = False
    while True:
        try:
            if not ready:
                with connect() as conn:
                    initialize(conn)
                ready = True
            with connect() as conn:
                done = tick(conn,run)
            if done:
                return
        except (psycopg.OperationalError, psycopg.InterfaceError, psycopg.errors.LockNotAvailable):
            pass
        time.sleep(2)


if __name__ == '__main__':
    main()
