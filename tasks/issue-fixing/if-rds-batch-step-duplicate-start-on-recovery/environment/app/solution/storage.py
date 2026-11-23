import os
import psycopg
from psycopg.types.json import Jsonb


def connect():
    return psycopg.connect(connect_timeout=3)


def event(conn, run, step, attempt, kind, message=None):
    conn.execute('INSERT INTO dagster.event_logs(run_id,step_key,attempt,dagster_event_type,event) VALUES (%s,%s,%s,%s,%s)',
                 (run, step, attempt, kind, Jsonb({'message': message})))


def events(conn, run, step):
    return conn.execute('SELECT attempt,dagster_event_type,pod_name FROM dagster.event_logs WHERE run_id=%s AND step_key=%s ORDER BY id', (run,step)).fetchall()


def job_name(run, step, attempt):
    return f'dagster-step-{run}-{step}' + (f'-{attempt-1}' if attempt > 1 else '')


def launch(conn, run, step, attempt):
    conn.execute('INSERT INTO dagster.k8s_jobs(job_name,run_id,step_key,attempt,argv) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                 (job_name(run,step,attempt),run,step,attempt,Jsonb(['python','-m','solution.execute_step'])))
