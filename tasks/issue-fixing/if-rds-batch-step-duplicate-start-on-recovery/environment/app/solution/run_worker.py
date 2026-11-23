"""Event-driven executor with periodic delegated-worker health checks."""
import argparse
import time
from harness.workflow import DEPENDENCIES, RETRIES
from .storage import connect, event, events, job_name, launch


def check_step_health(conn, run, step, attempt):
    name = job_name(run,step,attempt)
    job = conn.execute('SELECT job_name FROM dagster.k8s_jobs WHERE job_name=%s', (name,)).fetchone()
    if not job:
        return False
    pods = conn.execute('SELECT phase FROM dagster.k8s_pods WHERE job_name=%s', (name,)).fetchall()
    # Job controller's failed count: a replacement can still complete successfully.
    return not (len(pods) >= 2 and all(p[0] in ('Succeeded','Failed') for p in pods) and all(p[0] == 'Failed' for p in pods))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id',required=True)
    run = parser.parse_args().run_id
    with connect() as conn:
        conn.execute("UPDATE dagster.runs SET status='STARTED',updated_at=clock_timestamp() WHERE run_id=%s",(run,))
    done=set()
    submitted={}
    while len(done)<len(DEPENDENCIES):
        with connect() as conn:
            for step, deps in DEPENDENCIES.items():
                if step not in submitted and all(dep in done for dep in deps):
                    submitted[step]=1
                    launch(conn,run,step,1)
            for step,attempt in list(submitted.items()):
                if step in done:
                    continue
                history = events(conn,run,step)
                kinds = [k for a,k,_ in history if a == attempt]
                if 'STEP_SUCCESS' in kinds:
                    done.add(step)
                elif 'STEP_FAILURE' in kinds:
                    conn.execute("UPDATE dagster.runs SET status='FAILURE',updated_at=clock_timestamp() WHERE run_id=%s",(run,))
                    return
                elif 'STEP_UP_FOR_RETRY' in kinds:
                    submitted[step]=attempt+1
                    launch(conn,run,step,attempt+1)
                elif not check_step_health(conn,run,step,attempt):
                    event(conn,run,step,attempt,'STEP_UP_FOR_RETRY' if attempt <= RETRIES[step] else 'STEP_FAILURE','Delegated job unhealthy')
        time.sleep(2)
    with connect() as conn:
        conn.execute("UPDATE dagster.runs SET status='SUCCESS',updated_at=clock_timestamp() WHERE run_id=%s",(run,))


if __name__ == '__main__':
    main()
