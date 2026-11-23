"""RDS provisioning and the normal local Job controller."""
import os
import sys
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
import boto3
from botocore.config import Config
import psycopg
from psycopg import sql
from .transport import Relay

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cdk_seed.cfn_deploy import deploy


def provision():
    name = 'batch-' + uuid.uuid4().hex[:12]
    password = 'Master-' + uuid.uuid4().hex
    rds = boto3.client('rds', endpoint_url=os.environ['AWS_ENDPOINT_URL'],
                       config=Config(read_timeout=600, retries={'max_attempts': 1}))
    cfn = boto3.client('cloudformation', endpoint_url=os.environ['AWS_ENDPOINT_URL'],
                       config=Config(read_timeout=600, retries={'max_attempts': 1}))
    deploy(cfn, 'seed-' + name, Path(__file__).resolve().parents[2] / 'cdk_seed/seed.template.json',
           InstanceName=name, MasterPassword=password)
    deadline = time.monotonic()+180
    while True:
        info = rds.describe_db_instances(DBInstanceIdentifier=name)['DBInstances'][0]
        if info['DBInstanceStatus'] == 'available':
            break
        if time.monotonic()>deadline:
            raise RuntimeError('RDS not available')
        time.sleep(0.5)
    endpoint = info['Endpoint']
    config = dict(host=endpoint['Address'], port=endpoint['Port'], dbname='batchdb', user='master', password=password, connect_timeout=3)
    for _ in range(40):
        try:
            conn = psycopg.connect(**config,autocommit=True)
            break
        except psycopg.OperationalError:
            time.sleep(0.5)
    else:
        raise RuntimeError('RDS endpoint unreachable')
    conn.execute(Path(__file__).with_name('schema.sql').read_text())
    return name, config, conn


def worker_env(config):
    return {**os.environ,'PGHOST':str(config['host']),'PGPORT':str(config['port']),
            'PGDATABASE':config['dbname'],'PGSSLMODE':'disable','PGUSER':'run_worker','PGPASSWORD':'worker-local',
            'PYTHONPATH':os.environ.get('PYTHONPATH','/app'),'PYTHONDONTWRITEBYTECODE':'1'}


class Controller:
    def __init__(self, conn, config, run, root, seam=None):
        self.conn,self.config,self.run,self.root,self.seam = conn,config,run,Path(root),seam
        self.transport = Relay(config)
        self.pods = {}
        self.jobs = set()
        self.counter = 0

    def spawn(self, job):
        name,run,step,attempt,argv = job
        self.counter += 1
        pod = 'pod_'+uuid.uuid4().hex[:16]
        password = uuid.uuid4().hex
        self.conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} IN ROLE solver_pods').format(sql.Identifier(pod), sql.Literal(password)))
        self.conn.execute("INSERT INTO dagster.k8s_pods VALUES (%s,%s,'Running',NULL,NULL,'True')",(pod,name))
        env = worker_env(self.config)
        env.update(PGHOST='127.0.0.1', PGPORT=str(self.transport.server_address[1]), PGUSER=pod,PGPASSWORD=password,POD_NAME=pod,DAGSTER_RUN_ID=run,
                   DAGSTER_STEP_KEY=step,DAGSTER_STEP_ATTEMPT=str(attempt),
                   POD_BOOT_DELAY='7' if step=='extract' and attempt==1 else '0')
        if self.seam:
            env['SEAM_SOCKET']=self.seam
        cwd = tempfile.mkdtemp(prefix='pod-')
        log = open(self.root/(pod+'.log'),'w')
        process = subprocess.Popen(['python','-m','harness.pod',*argv],cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT)
        self.pods[pod] = dict(process=process,job=job,log=log)
        return pod

    def poll(self):
        for pod,state in self.pods.items():
            code = state['process'].poll()
            if code is not None:
                self.conn.execute("UPDATE dagster.k8s_pods SET phase=%s,exit_code=%s WHERE pod_name=%s AND deletion_ts IS NULL",
                                  ('Succeeded' if code==0 else 'Failed',code if code>=0 else 128-code,pod))
        jobs = self.conn.execute('SELECT job_name,run_id,step_key,attempt,argv FROM dagster.k8s_jobs WHERE run_id=%s ORDER BY created_at',(self.run,)).fetchall()
        for job in jobs:
            if job[0] not in self.jobs:
                self.jobs.add(job[0])
                self.spawn(job)

    def close(self):
        for state in self.pods.values():
            proc=state['process']
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            state['log'].close()
        self.transport.close()
