"""Independent controller and historical SQL observations; no submission imports."""
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import psycopg
from psycopg import sql
import pytest

RUNTIME=str(Path(__file__).parent/'runtime')
sys.path.insert(0,RUNTIME)
from harness.infra import provision,worker_env,Controller


@pytest.fixture(scope='session')
def database():
    try:
        _,config,conn=provision()
        conn.execute("CREATE ROLE probe_login LOGIN PASSWORD 'probe-password'")
        probe=psycopg.connect(**{**config,'user':'probe_login','password':'probe-password'})
        assert conn.execute('SELECT pg_terminate_backend(%s)',(probe.info.backend_pid,)).fetchone()[0]
        conn.execute('ALTER ROLE probe_login NOLOGIN')
        probe.close()
    except Exception as exc:
        pytest.fail('environment: RDS preflight failed: '+repr(exc))
    yield config,conn
    conn.close()


def exercise(database,mode):
    config,conn=database
    run=uuid.uuid4().hex[:12]
    root=Path('/logs/verifier')/run
    root.mkdir(parents=True)
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    address=tempfile.mkdtemp(prefix='driver-')+'/s'
    listener.bind(address)
    listener.listen(64)
    listener.settimeout(.01)
    controller=Controller(conn,config,run,root,address)
    # Pod processes also use the trusted, verifier-owned body implementation.
    old_path=os.environ.get('PYTHONPATH')
    os.environ['PYTHONPATH']=RUNTIME+':/app'
    conn.execute("INSERT INTO dagster.runs(run_id,status) VALUES (%s,'QUEUED')",(run,))
    log=open(root/'run-worker.log','w')
    executor=subprocess.Popen(['python','-m','solution.run_worker','--run-id',run],env=worker_env(config),
                              cwd=tempfile.mkdtemp(),stdout=log,stderr=subprocess.STDOUT)
    began=time.monotonic()
    fault=original=held=replacement=success_at=first_launch=None
    healed=False
    calls=[]
    errors=[]
    armed_batches=set()
    status='QUEUED'
    try:
        while time.monotonic()-began<145:
            controller.poll()
            now=time.monotonic()
            if controller.jobs and first_launch is None:
                first_launch=now
            status=conn.execute('SELECT status FROM dagster.runs WHERE run_id=%s',(run,)).fetchone()[0]
            if status=='SUCCESS' and success_at is None:
                success_at=now
            try:
                client,_=listener.accept()
            except socket.timeout:
                client=None
            if client:
                client.settimeout(5)
                raw=b''
                while b'\n' not in raw:
                    chunk=client.recv(4096)
                    if not chunk:
                        raise RuntimeError('incomplete body announcement')
                    raw+=chunk
                msg=json.loads(raw)
                calls.append(msg)
                if (mode=='commits' and msg['point']=='batch_insert' and msg['step']=='extract'
                        and msg['attempt']==1 and msg['batch'] in (1,2)
                        and msg['batch'] not in armed_batches):
                    action = 'inflight' if msg['batch']==1 else 'before'
                    controller.transport.arm(msg['pod'], action)
                    armed_batches.add(msg['batch'])
                target=(mode!='normal' and fault is None and msg['point']=='batch_insert'
                        and msg['step']=='extract' and msg['attempt']==1 and msg['batch']==3)
                if target:
                    original=msg['pod']
                    proc=controller.pods[original]['process']
                    if mode=='isolated':
                        proc.send_signal(signal.SIGSTOP)
                        limit=time.monotonic()+3
                        while time.monotonic()<limit:
                            stat=Path(f'/proc/{proc.pid}/stat').read_text().split(') ',1)[1].split()[0]
                            if stat=='T':
                                break
                            time.sleep(.01)
                        else:
                            raise RuntimeError('environment: pod failed to stop')
                        conn.execute(sql.SQL('ALTER ROLE {} NOLOGIN').format(sql.Identifier(original)))
                        conn.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s',(original,))
                        held=client
                    elif mode=='dead':
                        proc.kill()
                        proc.wait(timeout=5)
                        conn.execute("UPDATE dagster.k8s_pods SET phase='Failed',exit_code=137 WHERE pod_name=%s",(original,))
                        client.close()
                    else:
                        client.sendall(b'go')
                        client.close()
                    if mode in ('isolated','live','commits'):
                        conn.execute("UPDATE dagster.k8s_pods SET deletion_ts=clock_timestamp(),node_ready='Unknown' WHERE pod_name=%s",(original,))
                    fault=time.monotonic()
                else:
                    client.sendall(b'go')
                    client.close()
            now=time.monotonic()
            if fault is not None and replacement is None and now>=fault+1:
                replacement=controller.spawn(controller.pods[original]['job'])
            if mode=='isolated' and fault is not None and not healed and now>=fault+80:
                if controller.pods[original]['process'].poll() is not None:
                    errors.append('old process was terminated rather than allowed to return')
                conn.execute(sql.SQL('ALTER ROLE {} LOGIN').format(sql.Identifier(original)))
                with contextlib.suppress(OSError):
                    held.sendall(b'go')
                held.close()
                held=None
                with contextlib.suppress(ProcessLookupError):
                    controller.pods[original]['process'].send_signal(signal.SIGCONT)
                healed=True
            if mode=='isolated':
                if (fault is not None and now>=fault+95) or (fault is None and now-began>=67):
                    break
            elif success_at is not None and now>=success_at+2 and (mode=='normal' or replacement is not None):
                break
            elif (fault is not None and now>=fault+62) or (fault is None and now-began>=67):
                break
            time.sleep(.04)
        ev=conn.execute('SELECT id,step_key,attempt,pod_name,dagster_event_type FROM dagster.event_logs WHERE run_id=%s ORDER BY id',(run,)).fetchall()
        ledger=conn.execute('SELECT id,step_key,attempt,pod_name,batch_no FROM dagster.ledger WHERE run_id=%s ORDER BY id',(run,)).fetchall()
        jobs=conn.execute('SELECT step_key,attempt FROM dagster.k8s_jobs WHERE run_id=%s ORDER BY created_at',(run,)).fetchall()
        def need(predicate,message):
            if not predicate:
                errors.append(message)
        need(first_launch is not None and first_launch-began<=10,'startup did not submit extract within 10s')
        need(status=='SUCCESS','run did not finish SUCCESS: '+status)
        if mode in ('normal','live','commits'):
            need(success_at is not None and success_at-began<=65,'missed normal 65s objective')
        else:
            need(fault is not None and success_at is not None and success_at-fault<=60,'missed recovery 60s objective')
        if mode!='normal':
            need(fault is not None,'mid-extract write boundary not reached')
        extract=[e for e in ev if e[1]=='extract']
        success=[e for e in extract if e[4]=='STEP_SUCCESS']
        retries=[e for e in extract if e[4]=='STEP_UP_FOR_RETRY']
        expected=2 if mode in ('isolated','dead') else 1
        need(len(success)==1 and success[0][2]==expected,'extract must succeed exactly once on authoritative attempt')
        need(not any(e[4]=='STEP_FAILURE' for e in extract),'extract bypassed retry policy or stale writer completed')
        inv=[x for x in calls if x['point']=='body_enter' and x['step']=='extract']
        need(sorted(x['attempt'] for x in inv)==list(range(1,expected+1)),'duplicate body admission or missing body')
        need(len([e for e in extract if e[4] in ('STEP_START','STEP_RESTARTED')])==expected,'incorrect attempt boundaries')
        if expected==1:
            need(not retries and not any(s=='extract' and a>1 for s,a in jobs),'retried a live original')
            if original:
                need(all(x['pod']==original for x in inv),'replacement executed original body')
        else:
            old_rows=[e for e in ledger if e[1]=='extract' and e[2]==1]
            need(sorted(e[4] for e in old_rows)==[0,1,2],
                 'committed partial extract history was lost or changed')
            need(len(retries)==1 and retries[0][2]==1,'missing unique attempt-one retry closure')
            if len(retries)==1:
                close=retries[0][0]
                need(not any(e[1]=='extract' and e[2]==1 and e[0]>close for e in ledger),'abandoned worker wrote batches after closure')
            need(not any(e[2]==1 and e[4] in ('STEP_SUCCESS','STEP_FAILURE') for e in extract),'abandoned worker added terminal outcome')
        for step,count,attempt in [('extract',10,expected),('load',2,1),('audit',3,1),('publish',1,1)]:
            rows=[e for e in ledger if e[1]==step and e[2]==attempt]
            successes=[e for e in ev if e[1]==step and e[4]=='STEP_SUCCESS']
            need(sorted(e[4] for e in rows)==list(range(count)) and len({e[3] for e in rows})==1,step+' batch set incomplete or duplicated')
            need(len(successes)==1 and bool(rows) and successes[0][3]==rows[0][3],step+' success did not belong to batch writer')
        need(len([x for x in calls if x['point']=='body_enter' and x['step']=='load'])==1,'load body must run once')
        loads=[e for e in ev if e[1]=='load' and e[4] in ('STEP_START','STEP_RESTARTED')]
        need(bool(loads) and bool(success) and loads[0][0]>success[0][0],'load admitted before extract success')
        graph_checks(conn,run,ev,jobs,errors)
        if mode=='isolated':
            need(healed,'returning-worker observation missing')
        if mode=='commits':
            observed=[(x['action'],x['outcome']) for x in controller.transport.evidence]
            # The orphan COMMIT may resolve either way; the batch-set checks enforce exactly-once.
            need(sorted(observed) in (sorted([('inflight','committed'),('before','not_forwarded')]),
                                      sorted([('inflight','not_committed'),('before','not_forwarded')])),
                 'environment: did not observe both uncertain transaction outcomes: '+repr(observed))
        evidence=dict(mode=mode,run=run,status=status,events=ev,ledger=ledger,jobs=jobs,calls=calls,errors=errors,
                      transport=controller.transport.evidence, success_elapsed=None if success_at is None else success_at-began,
                      fault_elapsed=None if fault is None else fault-began)
        (root/'evidence.json').write_text(json.dumps(evidence,indent=2,default=str))
        return errors,root
    finally:
        if held:
            held.close()
        listener.close()
        if executor.poll() is None:
            executor.kill()
        executor.wait(timeout=10)
        controller.close()
        log.close()
        if old_path is None:
            os.environ.pop('PYTHONPATH',None)
        else:
            os.environ['PYTHONPATH']=old_path


@pytest.mark.parametrize('mode',['normal','live','isolated','dead','commits'])
def test_runner(database,mode):
    errors,root=exercise(database,mode)
    assert not errors, f'{mode}: {errors}; evidence: {root}'


# Exercise the supervisor/Job boundary, without importing any submission helpers.
def lifecycle(database, mode):
    from harness.supervisor import Supervisors
    config, conn = database
    run = uuid.uuid4().hex[:12]
    root = Path('/logs/verifier') / run
    root.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(tempfile.mkdtemp(prefix='lifecycle-') + '/s')
    listener.listen(64)
    listener.settimeout(.01)
    old_path = os.environ.get('PYTHONPATH')
    os.environ['PYTHONPATH'] = RUNTIME + ':/app'
    controller = Controller(conn, config, run, root, listener.getsockname())
    supervisors = Supervisors(config, run, root, replicas=2)
    conn.execute("INSERT INTO dagster.runs(run_id,status) VALUES (%s,'QUEUED')", (run,))
    supervisors.poll()
    began = time.monotonic()
    first_launch = terminal_at = final_loss = final_submission_age = None
    calls, errors, held, statuses = [], [], [], []
    boots, replaced = {}, set()
    crashes = set()
    healed = False
    expected = 'FAILURE' if mode in ('unstarted_exhausted', 'running_exhausted') else 'SUCCESS'
    try:
        while time.monotonic() - began < 125:
            controller.poll()
            now = time.monotonic()
            if controller.jobs and first_launch is None:
                first_launch = now
            status = conn.execute('SELECT status FROM dagster.runs WHERE run_id=%s', (run,)).fetchone()[0]
            statuses.append(status)
            supervisors.poll(status in ('SUCCESS','FAILURE'))
            # A monitor may fail while startup is pending, and again while a
            # retry closure or terminal outcome has just become durable.
            for point in (4, 8):
                if first_launch is not None and now >= first_launch + point and point not in crashes:
                    supervisors.restart()
                    crashes.add(point)
            retried = conn.execute("SELECT 1 FROM dagster.event_logs WHERE run_id=%s AND dagster_event_type='STEP_UP_FOR_RETRY'", (run,)).fetchone()
            if retried and 'retry' not in crashes:
                supervisors.restart()
                crashes.add('retry')
            if status in ('SUCCESS','FAILURE') and terminal_at is None:
                terminal_at = now
                final_submission_age = conn.execute("SELECT extract(epoch FROM clock_timestamp()-max(created_at)) FROM dagster.k8s_jobs WHERE run_id=%s AND step_key='extract'", (run,)).fetchone()[0]
                # A fresh monitor on a terminal run must leave it terminal.
                supervisors.restart()
                crashes.add('terminal')
            try:
                client, _ = listener.accept()
            except socket.timeout:
                client = None
            if client is not None:
                client.settimeout(5)
                raw = b''
                while b'\n' not in raw:
                    chunk = client.recv(4096)
                    if not chunk:
                        raise RuntimeError('incomplete boot/body announcement')
                    raw += chunk
                msg = json.loads(raw)
                calls.append(msg)
                step, attempt = msg['step'], msg['attempt']
                if msg['point']=='pod_boot' and step=='extract':
                    boots.setdefault(attempt, (now, msg['pod']))
                block = (msg['point']=='pod_boot' and step=='extract' and
                         (mode=='unstarted_exhausted' or (mode=='unstarted' and attempt==1)))
                kill = (mode=='running_exhausted' and msg['point']=='batch_insert' and
                        step=='extract' and msg['batch']==3)
                if block:
                    # No solution code or SQL has run yet. This is an image/node
                    # startup outage, not a body that can send contact.
                    held.append(client)
                    conn.execute("UPDATE dagster.k8s_pods SET deletion_ts=clock_timestamp(),node_ready='Unknown' WHERE pod_name=%s", (msg['pod'],))
                elif kill:
                    proc=controller.pods[msg['pod']]['process']
                    proc.kill()
                    proc.wait(timeout=5)
                    client.close()
                    conn.execute("UPDATE dagster.k8s_pods SET phase='Failed',exit_code=137 WHERE pod_name=%s", (msg['pod'],))
                    if attempt==2:
                        final_loss=time.monotonic()
                else:
                    client.sendall(b'go')
                    client.close()
            now=time.monotonic()
            for attempt, (booted, pod) in list(boots.items()):
                should_replace = mode=='running_exhausted' or mode=='unstarted_exhausted' or (mode=='unstarted' and attempt==1)
                if should_replace and attempt not in replaced and now >= booted+1:
                    controller.spawn(controller.pods[pod]['job'])
                    replaced.add(attempt)
            if mode.startswith('unstarted') and first_launch is not None and not healed and now>=first_launch+60:
                for client in held:
                    with contextlib.suppress(OSError):
                        client.sendall(b'go')
                    client.close()
                held.clear()
                healed=True
            if mode.startswith('unstarted'):
                if first_launch is not None and now>=first_launch+75:
                    break
            elif terminal_at is not None and now>=terminal_at+10:
                break
            elif now-began>=85:
                break
            time.sleep(.04)
        status = conn.execute('SELECT status FROM dagster.runs WHERE run_id=%s', (run,)).fetchone()[0]
        ev=conn.execute('SELECT id,step_key,attempt,pod_name,dagster_event_type FROM dagster.event_logs WHERE run_id=%s ORDER BY id', (run,)).fetchall()
        ledger=conn.execute('SELECT id,step_key,attempt,pod_name,batch_no FROM dagster.ledger WHERE run_id=%s ORDER BY id', (run,)).fetchall()
        jobs=conn.execute('SELECT step_key,attempt FROM dagster.k8s_jobs WHERE run_id=%s ORDER BY created_at', (run,)).fetchall()
        def need(value, message):
            if not value:
                errors.append(message)
        need(first_launch is not None and first_launch-began<=10, 'startup did not submit extract within 10s')
        need(status==expected, 'run did not reach '+expected+': '+status)
        if terminal_at is not None:
            first_terminal=next(i for i,s in enumerate(statuses) if s in ('SUCCESS','FAILURE'))
            need(all(s==expected for s in statuses[first_terminal:]), 'terminal run outcome changed on monitor restart')
        extract=[e for e in ev if e[1]=='extract']
        closures=[e for e in extract if e[4] in ('STEP_UP_FOR_RETRY','STEP_FAILURE')]
        bodies=[x for x in calls if x['point']=='body_enter']
        starts=[e[2] for e in extract if e[4] in ('STEP_START','STEP_RESTARTED')]
        if mode.startswith('unstarted'):
            # Startup allowance is distinct from running-worker contact expiry.
            early=conn.execute("""SELECT e.id FROM dagster.event_logs e JOIN dagster.k8s_jobs j
                ON (j.run_id,j.step_key,j.attempt)=(e.run_id,e.step_key,e.attempt)
                WHERE e.run_id=%s AND e.dagster_event_type IN ('STEP_UP_FOR_RETRY','STEP_FAILURE')
                AND e.ts < j.created_at + interval '10 seconds'""", (run,)).fetchall()
            need(not early, 'spent a retry before the Job admission allowance expired')
            need(healed, 'late startup observation missing')
            if mode=='unstarted':
                need([(e[2],e[4]) for e in closures]==[(1,'STEP_UP_FOR_RETRY')], 'unstarted Job missing unique retry closure')
                need(starts==[2], 'invented a start for an unstarted Job or admitted stale launch')
                need(sorted((x['step'],x['attempt']) for x in bodies)==[('audit',1),('extract',2),('load',1),('publish',1)], 'late or duplicate body admission')
                need(terminal_at is not None and first_launch is not None and terminal_at-first_launch<=60, 'missed unstarted Job 60s recovery objective')
                need(not any(e[1]=='extract' and e[2]==1 for e in ledger), 'closed unstarted Job wrote batches')
            else:
                need([(e[2],e[4]) for e in closures]==[(1,'STEP_UP_FOR_RETRY'),(2,'STEP_FAILURE')], 'unstarted retry exhaustion did not close once')
                need(not starts and not bodies and not ledger, 'exhausted or stale unstarted Job entered body or invented work')
                need(final_submission_age is not None and final_submission_age<=25, 'missed unstarted exhaustion objective')
        elif mode=='running_exhausted':
            need([(e[2],e[4]) for e in closures]==[(1,'STEP_UP_FOR_RETRY'),(2,'STEP_FAILURE')], 'running retry exhaustion did not close once')
            need(starts==[1,2], 'incorrect running attempt boundaries')
            need(sorted((x['step'],x['attempt']) for x in bodies)==[('extract',1),('extract',2)], 'running failure admitted extra or missing body')
            for attempt in (1,2):
                need(sorted(e[4] for e in ledger if e[1]=='extract' and e[2]==attempt)==[0,1,2], 'running failure lost committed partial history')
            need(final_loss is not None and terminal_at is not None and terminal_at-final_loss<=15, 'missed running exhaustion objective')
        else:
            need(not closures and starts==[1], 'cold startup or monitor restart spent a step retry')
            need(sorted((x['step'],x['attempt']) for x in bodies)==[('audit',1),('extract',1),('load',1),('publish',1)], 'cold startup admitted duplicate body')
            need(terminal_at is not None and terminal_at-began<=65, 'missed cold startup 65s objective')
        if expected=='SUCCESS':
            attempt=2 if mode=='unstarted' else 1
            for step,count,a in [('extract',10,attempt),('load',2,1),('audit',3,1),('publish',1,1)]:
                rows=[e for e in ledger if e[1]==step and e[2]==a]
                successes=[e for e in ev if e[1]==step and e[4]=='STEP_SUCCESS']
                need(sorted(e[4] for e in rows)==list(range(count)) and len({e[3] for e in rows})==1, step+' batch set incomplete or duplicated')
                need(len(successes)==1 and bool(rows) and successes[0][3]==rows[0][3], step+' success did not belong to batch writer')
            success=[e for e in extract if e[4]=='STEP_SUCCESS']
            load=[e for e in ev if e[1]=='load' and e[4]=='STEP_START']
            need(len(success)==1 and len(load)==1 and load[0][0]>success[0][0], 'load ran before authoritative extract success')
            graph_checks(conn,run,ev,jobs,errors)
        else:
            need(not any(e[4]=='STEP_SUCCESS' for e in ev), 'invented success after retry exhaustion')
            need(not any(s=='load' or a>2 for s,a in jobs), 'exhausted run submitted more work')
        (root/'evidence.json').write_text(json.dumps(dict(mode=mode,run=run,status=status,
            events=ev,ledger=ledger,jobs=jobs,calls=calls,errors=errors,crashes=sorted(map(str,crashes)),
            supervisor_processes=len(supervisors.all),
            terminal_elapsed=None if terminal_at is None else terminal_at-began,
            first_launch_elapsed=None if first_launch is None else first_launch-began), indent=2, default=str))
        return errors, root
    finally:
        for client in held:
            client.close()
        listener.close()
        supervisors.close()
        controller.close()
        if old_path is None:
            os.environ.pop('PYTHONPATH',None)
        else:
            os.environ['PYTHONPATH']=old_path


@pytest.mark.parametrize('mode',['cold','unstarted','unstarted_exhausted','running_exhausted'])
def test_lifecycle(database, mode):
    errors, root = lifecycle(database, mode)
    assert not errors, f'{mode}: {errors}; evidence: {root}'


def graph_checks(conn, run, ev, jobs, errors):
    def need(value, message):
        if not value:
            errors.append(message)
    expected={'extract':(), 'load':('extract',), 'audit':('extract',), 'publish':('load','audit')}
    for step,deps in expected.items():
        starts=[e[0] for e in ev if e[1]==step and e[4] in ('STEP_START','STEP_RESTARTED')]
        success=[e[0] for e in ev if e[1]==step and e[4]=='STEP_SUCCESS']
        need(len(success)==1,step+' did not succeed once')
        for dep in deps:
            upstream=[e[0] for e in ev if e[1]==dep and e[4]=='STEP_SUCCESS']
            need(bool(starts) and len(upstream)==1 and min(starts)>upstream[0],step+' admitted before '+dep+' success')
    # Both branches must be submitted before waiting for either to complete.
    # This compares observable ordering, not a narrow wall-clock threshold.
    submitted=conn.execute("SELECT step_key,created_at FROM dagster.k8s_jobs WHERE run_id=%s AND step_key IN ('load','audit')",(run,)).fetchall()
    ended=conn.execute("SELECT min(ts) FROM dagster.event_logs WHERE run_id=%s AND step_key IN ('load','audit') AND dagster_event_type='STEP_SUCCESS'",(run,)).fetchone()[0]
    need({r[0] for r in submitted}=={'load','audit'} and ended is not None and all(r[1]<ended for r in submitted),
         'serialized independent branches instead of submitting both')


@pytest.mark.parametrize('mode',['returning','pending','resolving','suspended'])
def test_branch_shutdown(database,mode):
    from harness.supervisor import Supervisors
    config,conn=database
    run=uuid.uuid4().hex[:12]
    root=Path('/logs/verifier')/run
    root.mkdir(parents=True)
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    listener.bind(tempfile.mkdtemp(prefix='branches-')+'/s')
    listener.listen(64)
    listener.settimeout(.01)
    old_path=os.environ.get('PYTHONPATH')
    os.environ['PYTHONPATH']=RUNTIME+':/app'
    controller=Controller(conn,config,run,root,listener.getsockname())
    supervisors=Supervisors(config,run,root,replicas=2)
    conn.execute("INSERT INTO dagster.runs(run_id,status) VALUES (%s,'QUEUED')",(run,))
    conn.execute('INSERT INTO dagster.run_inputs VALUES (%s,true)',(run,))
    supervisors.poll()
    began=time.monotonic()
    target=error_client=terminal_at=failure_at=None
    held=[]
    calls=[]
    errors=[]
    ready=healed=False
    terminal_snapshot=None
    commit_witness=None
    crashes=set()
    try:
        while time.monotonic()-began<100:
            controller.poll()
            now=time.monotonic()
            status=conn.execute('SELECT status FROM dagster.runs WHERE run_id=%s',(run,)).fetchone()[0]
            supervisors.poll(status in ('SUCCESS','FAILURE'))
            failed=conn.execute("SELECT id,ts FROM dagster.event_logs WHERE run_id=%s AND step_key='load' AND dagster_event_type='STEP_FAILURE'",(run,)).fetchone()
            if failed and failure_at is None:
                failure_at=now
                supervisors.restart()
                crashes.add('failure')
            if failure_at is not None and now>=failure_at+3 and 'drain' not in crashes:
                supervisors.restart()
                crashes.add('drain')
            if status in ('SUCCESS','FAILURE') and terminal_at is None:
                terminal_at=now
                terminal_snapshot=(conn.execute('SELECT * FROM dagster.ledger WHERE run_id=%s ORDER BY id',(run,)).fetchall(),
                                   conn.execute("SELECT * FROM dagster.event_logs WHERE run_id=%s AND dagster_event_type<>'ENGINE_EVENT' ORDER BY id",(run,)).fetchall(),
                                   conn.execute('SELECT * FROM dagster.k8s_jobs WHERE run_id=%s ORDER BY job_name',(run,)).fetchall())
                supervisors.restart()
                crashes.add('terminal')
            try:
                client,_=listener.accept()
            except socket.timeout:
                client=None
            if client:
                client.settimeout(5)
                raw=b''
                while b'\n' not in raw:
                    chunk=client.recv(4096)
                    if not chunk:
                        raise RuntimeError('incomplete branch announcement')
                    raw+=chunk
                msg=json.loads(raw)
                calls.append(msg)
                if msg['point']=='body_error':
                    error_client=client
                elif mode=='pending' and msg['point']=='pod_boot' and msg['step']=='audit' and not healed:
                    target=target or msg['pod']
                    held.append(client)
                    ready=True
                elif mode in ('returning','suspended') and msg['point']=='batch_insert' and msg['step']=='audit' and msg['batch']==1 and target is None:
                    target=msg['pod']
                    proc=controller.pods[target]['process']
                    proc.send_signal(signal.SIGSTOP)
                    deadline=time.monotonic()+3
                    while time.monotonic()<deadline:
                        if Path(f'/proc/{proc.pid}/stat').read_text().split(') ',1)[1].split()[0]=='T':
                            break
                        time.sleep(.01)
                    else:
                        raise RuntimeError('environment: audit did not stop')
                    if mode=='returning':
                        conn.execute(sql.SQL('ALTER ROLE {} NOLOGIN').format(sql.Identifier(target)))
                        conn.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s',(target,))
                    # 'suspended' freezes the peer inside the same write but leaves its
                    # database sessions connected, with that batch transaction open.
                    conn.execute("UPDATE dagster.k8s_pods SET deletion_ts=clock_timestamp(),node_ready='Unknown' WHERE pod_name=%s",(target,))
                    held.append(client)
                    ready=True
                else:
                    if mode=='resolving' and msg['point']=='batch_insert' and msg['step']=='audit' and msg['batch']==0 and target is None:
                        target=msg['pod']
                        controller.transport.arm(target,'inflight')
                    client.sendall(b'go')
                    client.close()
            # Observe a real unresolved COMMIT, rather than guessing at elapsed
            # time or requiring the submission to use an internal lock shape.
            if mode=='resolving' and target:
                with controller.transport.lock:
                    consumed=target not in controller.transport.armed
                if consumed:
                    # Forwarded COMMIT may already have resolved in a correct
                    # implementation (e.g. one using immediate validation).
                    # A live SQL witness is evidence, not a required SQL shape.
                    if commit_witness is None:
                        active=conn.execute("SELECT pid,state,query,wait_event FROM pg_stat_activity WHERE usename=%s AND state='active' AND wait_event='PgSleep'",(target,)).fetchall()
                        if active:
                            commit_witness=active
                    ready=True
            if ready and error_client is not None:
                error_client.sendall(b'go')
                error_client.close()
                error_client=None
            now=time.monotonic()
            if terminal_at is not None and now>=terminal_at+4 and not healed:
                if target:
                    if mode in ('returning','suspended'):
                        if controller.pods[target]['process'].poll() is not None:
                            errors.append('cancelled original was terminated rather than allowed to return')
                        if mode=='returning':
                            conn.execute(sql.SQL('ALTER ROLE {} LOGIN').format(sql.Identifier(target)))
                        controller.pods[target]['process'].send_signal(signal.SIGCONT)
                    for client in held:
                        with contextlib.suppress(OSError):
                            client.sendall(b'go')
                        client.close()
                    held.clear()
                    controller.spawn(controller.pods[target]['job'])
                healed=True
            # The resumed peer's open batch still has its nine-second transformation to run.
            if terminal_at is not None and now>=terminal_at+(20 if mode=='suspended' else 15):
                break
            if failure_at is not None and now>=failure_at+45:
                break
            time.sleep(.04)
        status,terminal_ts=conn.execute('SELECT status,updated_at FROM dagster.runs WHERE run_id=%s',(run,)).fetchone()
        ev=conn.execute('SELECT id,step_key,attempt,pod_name,dagster_event_type FROM dagster.event_logs WHERE run_id=%s ORDER BY id',(run,)).fetchall()
        ledger=conn.execute('SELECT id,step_key,attempt,pod_name,batch_no FROM dagster.ledger WHERE run_id=%s ORDER BY id',(run,)).fetchall()
        jobs=conn.execute('SELECT step_key,attempt FROM dagster.k8s_jobs WHERE run_id=%s ORDER BY created_at',(run,)).fetchall()
        def need(value,message):
            if not value:
                errors.append(message)
        need(ready and target is not None, 'did not reach concurrent peer schedule')
        need(status=='FAILURE', 'failed branch did not close run FAILURE: '+status)
        failures=[e for e in ev if e[4]=='STEP_FAILURE']
        cancellations=[e for e in ev if e[4]=='STEP_CANCELLED']
        need([(e[1],e[2]) for e in failures]==[('load',1)], 'body error did not produce one load failure')
        need([(e[1],e[2]) for e in cancellations]==[('audit',1)], 'unfinished audit did not get one cancellation closure')
        if len(failures)==1:
            age=conn.execute('SELECT extract(epoch FROM (%s-ts)) FROM dagster.event_logs WHERE id=%s',(terminal_ts,failures[0][0])).fetchone()[0]
            need(status=='FAILURE' and 0<=age<=25, 'missed 25s branch shutdown objective')
        need(not any(s=='publish' or a!=1 for s,a in jobs), 'failed workflow submitted downstream work or spent a peer retry')
        need(not any(e[1]=='audit' and e[4] in ('STEP_SUCCESS','STEP_FAILURE','STEP_UP_FOR_RETRY') for e in ev), 'cancelled peer reported a stale outcome')
        if len(cancellations)==1:
            close=cancellations[0][0]
            need(not any(r[1]=='audit' and r[0]>close for r in ledger), 'peer wrote after cancellation')
            need(not any(e[1]=='audit' and e[0]>close and e[4]!='ENGINE_EVENT' for e in ev), 'peer appended a workflow event after cancellation')
        bodies=[x for x in calls if x['point']=='body_enter']
        need(sorted(x['step'] for x in bodies)==(['extract','load'] if mode=='pending' else ['audit','extract','load']), 'late, duplicate or missing branch body')
        need(sorted(r[4] for r in ledger if r[1]=='extract')==list(range(10)), 'completed extract was changed during shutdown')
        need([r[4] for r in ledger if r[1]=='load']==[0], 'failed load lost or replayed its committed partial batch')
        audit=[r[4] for r in ledger if r[1]=='audit']
        if mode=='pending':
            need(not audit and not any(e[1]=='audit' and e[4]=='STEP_START' for e in ev), 'unstarted peer invented a start or work')
        elif mode in ('returning','suspended'):
            need(audit==[0], 'cancelled audit lost partial history or wrote after return')
        else:
            outcomes=[e['outcome'] for e in controller.transport.evidence if e['action']=='inflight']
            need(len(outcomes)==1 and outcomes[0] in ('committed','not_committed'),
                 'environment: unresolved peer COMMIT was not observed to resolve')
            need(len(audit)==len(set(audit)), 'unresolved peer batch duplicated')
            if outcomes==['committed']:
                need(0 in audit, 'committed peer batch disappeared')
        need(healed, 'late peer/replacement observation missing')
        if terminal_snapshot is not None:
            final=(conn.execute('SELECT * FROM dagster.ledger WHERE run_id=%s ORDER BY id',(run,)).fetchall(),
                   conn.execute("SELECT * FROM dagster.event_logs WHERE run_id=%s AND dagster_event_type<>'ENGINE_EVENT' ORDER BY id",(run,)).fetchall(),
                   conn.execute('SELECT * FROM dagster.k8s_jobs WHERE run_id=%s ORDER BY job_name',(run,)).fetchall())
            need(final==terminal_snapshot, 'history changed after terminal run or monitor restart')
        late=conn.execute("SELECT id FROM dagster.ledger WHERE run_id=%s AND ts>%s UNION ALL SELECT id FROM dagster.event_logs WHERE run_id=%s AND ts>%s AND dagster_event_type<>'ENGINE_EVENT'",(run,terminal_ts,run,terminal_ts)).fetchall()
        need(not late, 'an admitted transaction wrote history after terminal run')
        (root/'evidence.json').write_text(json.dumps(dict(mode=mode,run=run,status=status,events=ev,ledger=ledger,jobs=jobs,calls=calls,
            errors=errors,transport=controller.transport.evidence,commit_witness=commit_witness,crashes=sorted(crashes),
            terminal_elapsed=None if terminal_at is None else terminal_at-began,
            failure_elapsed=None if failure_at is None else failure_at-began),indent=2,default=str))
        assert not errors,f'{mode}: {errors}; evidence: {root}'
    finally:
        if error_client:
            error_client.close()
        for client in held:
            client.close()
        if mode in ('returning','suspended') and target:
            with contextlib.suppress(ProcessLookupError):
                controller.pods[target]['process'].send_signal(signal.SIGCONT)
        listener.close()
        supervisors.close()
        controller.close()
        if old_path is None:
            os.environ.pop('PYTHONPATH',None)
        else:
            os.environ['PYTHONPATH']=old_path
