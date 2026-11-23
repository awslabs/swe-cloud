import os
import threading
import time
import psycopg
from harness import step_body
from harness.workflow import RETRIES
from .storage import connect, event
from .authority import claim, authorized, holds, renew, Revoked


class Context:
    def __init__(self, run, step, attempt, owner):
        self.args = run, step, attempt, owner

    def commit_batch(self, batch_no, write):
        run, step, attempt, owner = self.args
        while True:
            try:
                with connect() as conn:
                    # An unknown COMMIT does not grant readmission: a revoked
                    # worker stops here, before the batch SQL is replayed.
                    holds(conn, *self.args)
                    # The previous server transaction may still be committing
                    # after its client socket failed. Serialize each receipt
                    # identity across that unresolved COMMIT, then take a new
                    # READ COMMITTED snapshot in a separate statement.
                    key = f'{run}/{step}/{attempt}/{batch_no}'
                    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (key,))
                    receipt = conn.execute('''SELECT id FROM dagster.ledger
                        WHERE run_id=%s AND step_key=%s AND attempt=%s AND batch_no=%s''',
                        (run,step,attempt,batch_no)).fetchone()
                    if receipt is None:
                        write(conn)
                    # Fence after the batch SQL, immediately before COMMIT. Nothing is
                    # locked while `write` runs, so a worker frozen inside it cannot
                    # hold up its own cancellation or retry closure; it finds the
                    # attempt closed here when it returns and rolls back. From here
                    # to the end of COMMIT the share lock makes a closure wait.
                    holds(conn, *self.args, lock='FOR SHARE')
                return
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # Either outcome is possible. The next transaction checks both
                # the current fence and a durable receipt before replaying SQL.
                time.sleep(.2)

    def finish(self, error=None):
        while True:
            try:
                with authorized(*self.args, exclusive=True) as conn:
                    run, step, attempt, _ = self.args
                    event(conn,run,step,attempt,'STEP_SUCCESS' if error is None else ('STEP_UP_FOR_RETRY' if attempt<=RETRIES[step] else 'STEP_FAILURE'),error)
                    conn.execute('UPDATE solver.attempts SET owner=NULL,current_attempt=%s WHERE run_id=%s AND step_key=%s', (attempt+(error is not None),run,step))
                return
            except (psycopg.OperationalError, psycopg.InterfaceError):
                time.sleep(.2)


def main():
    args = (os.environ['DAGSTER_RUN_ID'], os.environ['DAGSTER_STEP_KEY'],
            int(os.environ['DAGSTER_STEP_ATTEMPT']), os.environ['POD_NAME'])
    try:
        with connect() as conn:
            if not claim(conn,*args):
                return
    except psycopg.errors.LockNotAvailable:
        return
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(1):
            try:
                renew(*args)
            except Revoked:
                return
            except psycopg.Error:
                pass
    thread = threading.Thread(target=heartbeat,daemon=True)
    thread.start()
    try:
        ctx = Context(*args)
        step_body.run(args[1], args[2], ctx)
        ctx.finish()
    except Revoked:
        pass
    except Exception as exc:
        try:
            ctx.finish(str(exc))
        except Revoked:
            pass
    finally:
        stop.set()
        thread.join(timeout=4)


if __name__ == '__main__':
    main()
