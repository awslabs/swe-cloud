"""The batch application's transaction interface. No worker coordination here."""
import json
import os
import socket
import time
from .workflow import BATCHES


def announce(point, **facts):
    path = os.environ.get('SEAM_SOCKET')
    if path:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(path)
            sock.sendall((json.dumps(dict(point=point, pod=os.environ['POD_NAME'], **facts))+'\n').encode())
            if not sock.recv(64):
                raise RuntimeError('driver disconnected')


def write_batch(conn, run_id, step_key, attempt, batch_no, payload):
    announce('batch_insert', step=step_key, attempt=attempt, batch=batch_no)
    if (step_key == 'extract' and batch_no == 4) or (step_key == 'audit' and batch_no == 1):
        # Representative slow database-side batch transformation.
        conn.execute('SELECT pg_sleep(9)')
    conn.execute('INSERT INTO dagster.ledger(run_id,step_key,attempt,batch_no,payload) VALUES (%s,%s,%s,%s,%s)',
                 (run_id, step_key, attempt, batch_no, payload))


def run(step_key, attempt, ctx):
    announce('body_enter', step=step_key, attempt=attempt)
    count = BATCHES[step_key]
    for batch in range(count):
        # Input rejection is an application error, unlike a lost SQL session.
        # The body owns validation; the orchestration owns its attempt outcome.
        if step_key == 'load' and batch == 1:
            import psycopg
            with psycopg.connect(connect_timeout=3) as conn:
                rejected = conn.execute('SELECT reject_load FROM dagster.run_inputs WHERE run_id=%s',
                                        (os.environ['DAGSTER_RUN_ID'],)).fetchone()
            if rejected == (True,):
                announce('body_error', step=step_key, attempt=attempt)
                raise ValueError('load input failed validation')
        def write(conn):
            write_batch(conn, os.environ['DAGSTER_RUN_ID'], step_key, attempt, batch, f'{step_key}:{batch}')
        ctx.commit_batch(batch, write)
        time.sleep(1)
