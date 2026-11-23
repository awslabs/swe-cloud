"""Step worker entrypoint, including launch-time attempt verification."""
import os
import time
import psycopg
from harness import step_body
from harness.workflow import RETRIES
from .storage import connect, event, events


def verify_step(conn, run, step, attempt):
    stats = events(conn, run, step)
    if attempt == 1 and stats:
        event(conn, run, step, attempt, 'ENGINE_EVENT', f'Attempted to run {step} again even though it was already started')
        return False
    retries = sum(kind == 'STEP_UP_FOR_RETRY' for _,kind,_ in stats)
    if attempt > 1 and (not stats or retries != attempt - 1 or any(a == attempt and k in ('STEP_START','STEP_RESTARTED') for a,k,_ in stats)):
        event(conn, run, step, attempt, 'ENGINE_EVENT', 'Attempt does not match event history')
        return False
    return True


class Context:
    def commit_batch(self, batch_no, write):
        while True:
            try:
                with connect() as conn:
                    write(conn)
                return
            except (psycopg.OperationalError, psycopg.InterfaceError):
                time.sleep(.2)


def main():
    run,step,attempt = os.environ['DAGSTER_RUN_ID'],os.environ['DAGSTER_STEP_KEY'],int(os.environ['DAGSTER_STEP_ATTEMPT'])
    with connect() as conn:
        if not verify_step(conn,run,step,attempt):
            return
        event(conn,run,step,attempt,'STEP_START' if attempt == 1 else 'STEP_RESTARTED')
    try:
        step_body.run(step,attempt,Context())
    except Exception as exc:
        with connect() as conn:
            event(conn,run,step,attempt,'STEP_UP_FOR_RETRY' if attempt<=RETRIES[step] else 'STEP_FAILURE',str(exc))
        raise
    with connect() as conn:
        event(conn,run,step,attempt,'STEP_SUCCESS')


if __name__ == '__main__':
    main()
