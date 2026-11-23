"""Process supervisor; all run and step decisions belong to solution.run_worker."""
import subprocess
import tempfile
import uuid
from .infra import worker_env


class Supervisors:
    def __init__(self, config, run, root, replicas=1):
        self.config,self.run,self.root,self.replicas = config,run,root,replicas
        self.active=[]
        self.all=[]

    def poll(self, terminal=False):
        self.active=[p for p in self.active if p.poll() is None]
        if terminal:
            return
        while len(self.active)<self.replicas:
            identity=uuid.uuid4().hex[:12]
            log=open(self.root/('executor_'+identity+'.log'),'w')
            env=worker_env(self.config)
            env['PGAPPNAME']='executor_'+identity
            proc=subprocess.Popen(['python','-m','solution.run_worker','--run-id',self.run],
                                  env=env,cwd=tempfile.mkdtemp(prefix='executor-'),
                                  stdout=log,stderr=subprocess.STDOUT)
            self.active.append(proc)
            self.all.append((proc,log))

    def restart(self):
        for proc in self.active:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
        self.active=[]
        self.poll()

    def close(self):
        for proc,log in self.all:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            log.close()
