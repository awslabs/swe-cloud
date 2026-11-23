"""Launch the normal batch workload on a fresh local RDS instance."""
import argparse
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from .infra import provision, Controller
from .supervisor import Supervisors


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout',type=float,default=120)
    parser.add_argument('--reject-load', action='store_true', help='exercise application input rejection')
    args=parser.parse_args()
    name,config,conn=provision()
    run=uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO dagster.runs(run_id,status) VALUES (%s,'QUEUED')",(run,))
    conn.execute('INSERT INTO dagster.run_inputs VALUES (%s,%s)', (run,args.reject_load))
    print(json.dumps(dict(resource=name,run_id=run,host=config['host'],port=config['port'],database=config['dbname'])),flush=True)
    root=Path(tempfile.mkdtemp(prefix='controller-'))
    controller=Controller(conn,config,run,root)
    supervisors=Supervisors(config,run,root)
    supervisors.poll()
    try:
        deadline=time.monotonic()+args.timeout
        while time.monotonic()<deadline:
            supervisors.poll()
            controller.poll()
            status=conn.execute('SELECT status FROM dagster.runs WHERE run_id=%s',(run,)).fetchone()[0]
            if status in ('SUCCESS','FAILURE'):
                print(status,flush=True)
                return
            time.sleep(.2)
        raise RuntimeError('run timed out; pod logs: '+str(root))
    finally:
        supervisors.close()
        controller.close()


if __name__=='__main__':
    main()
