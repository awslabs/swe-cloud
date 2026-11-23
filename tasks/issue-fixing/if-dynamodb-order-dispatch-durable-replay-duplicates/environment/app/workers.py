"""The scheduler's process lifecycle; dispatch tickets are durable runtime state."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
from aws import client


class Workers:
    def __init__(self):
        self.retired = []

    def start(self, request, directory):
        self.reap()
        request = {**request, 'invocation_id': str(uuid.uuid4())}
        client().put_item(TableName=os.environ.get('EXECUTION_TABLE', 'durable_executions'),
                          Item={'execution_id': {'S': request['execution_id']},
                                'active_invocation_id': {'S': request['invocation_id']}})
        path = Path(directory) / 'request.json'
        path.write_text(json.dumps(request))
        return subprocess.Popen([sys.executable, '/app/invoke.py', '--request', str(path)],
                                cwd=directory, env={**os.environ, 'PYTHONPATH': '/app'},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def retire(self, process):
        # SIGTERM requests cooperative retirement; an outstanding callback may
        # unwind while a newly ticketed invocation starts. SIGKILL bounds the tail.
        process.send_signal(signal.SIGTERM)
        self.retired.append((time.monotonic() + 3, process))

    def reap(self, force=False):
        remaining = []
        for until, process in self.retired:
            if process.poll() is None and (force or time.monotonic() >= until):
                process.kill()
            if process.poll() is not None or force:
                process.communicate()
            else:
                remaining.append((until, process))
        self.retired = remaining

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.reap(force=True)
