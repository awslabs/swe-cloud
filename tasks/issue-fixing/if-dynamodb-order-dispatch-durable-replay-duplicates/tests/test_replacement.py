import json
from decimal import Decimal
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import pytest
from test_dispatch import APP, BOOTSTRAP, LOG_ROOT, Deployment
from transport import Link


class Worker:
    def __init__(self, deployment, request, endpoint):
        self.directory = tempfile.TemporaryDirectory(prefix='replacement-')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.directory.name + '/gate')
        self.listener.listen(4)
        self.result_budget = max(10, request.get('invocation_timeout_s', 4) + 3)
        self.entered = False
        self.connection = None
        env = {**os.environ, **deployment.tables, 'AWS_ENDPOINT_URL': endpoint,
               'PYTHONPATH': APP, 'PYTHONDONTWRITEBYTECODE': '1',
               'SEAM_SOCKET': self.directory.name + '/gate'}
        self.process = subprocess.Popen([sys.executable, BOOTSTRAP, json.dumps(request)],
                                        cwd=self.directory.name, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def accept_handler(self):
        ready, _, _ = select.select([self.listener], [], [], 8)
        assert ready, 'worker did not reach handler: ' + repr(self.process.poll())
        self.connection, _ = self.listener.accept()
        self.connection.settimeout(5)
        data = b''
        while b'\n' not in data:
            part = self.connection.recv(4096)
            assert part, data
            data += part
        announcement = json.loads(data.splitlines()[0])
        assert announcement['seam'] == 'handler_entry', announcement
        self.entered = True

    def resume(self):
        if self.connection:
            self.connection.sendall(b'continue')
            self.connection.close()
            self.connection = None

    def result(self):
        # If an incorrectly admitted worker reaches the handler, let it finish so
        # the durable-state assertions still run rather than hanging on the gate.
        end = time.monotonic() + self.result_budget
        while self.process.poll() is None and time.monotonic() < end:
            ready, _, _ = select.select([self.listener], [], [], 0.05)
            if ready:
                self.accept_handler()
                self.resume()
        stdout, stderr = self.process.communicate(timeout=2)
        assert self.process.returncode == 0, (stdout, stderr)
        return json.loads(stdout.strip().splitlines()[-1])

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.communicate()
        if self.connection:
            self.connection.close()
        self.listener.close()
        self.directory.cleanup()


@pytest.fixture(scope='module', params=[('acquire', False), ('complete', False), ('delete', False),
                        ('complete', True), ('delete', True)])
def replacement(request):
    operation, successor_runs = request.param
    deployment = Deployment()
    event = {'order': uuid.uuid4().hex, 'warehouse': 'central'}
    original = deployment.request(event, timeout_ms=90000, wait_s=0)
    if operation == 'delete':
        original['handler_error'] = True
    else:
        original['direct_result'] = {'receipt': uuid.uuid4().hex, 'worker': 'original'}
    original = deployment.issue(original)
    evidence = {'operation': operation, 'successor_runs': successor_runs,
                'request': original, 'event': event}
    with Link(os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566'),
              deployment.tables['IDEMPOTENCY_TABLE']) as link:
        if operation == 'acquire':
            link.armed.set()
        worker = Worker(deployment, original, link.endpoint)
        try:
            if operation != 'acquire':
                worker.accept_handler()
                link.armed.set()
                worker.resume()
            assert link.pending.wait(8), 'no in-flight order mutation to delay'
            evidence['before'] = deployment.item(event)
            # Only the runtime registry changes here: the new worker has not yet
            # touched the order record. This is a real dispatch gap, not a takeover
            # inferred from a newer order record.
            successor = {**original, 'direct_result': {'receipt': uuid.uuid4().hex, 'worker': 'replacement'}}
            successor.pop('handler_error', None)
            successor = deployment.issue(successor)
            evidence['successor'] = successor
            if successor_runs:
                evidence['successor_result'] = deployment.invoke(successor)
                evidence['committed'] = deployment.item(event)
            evidence['registry_before_release'] = deployment.db.get_item(
                TableName=deployment.tables['EXECUTION_TABLE'],
                Key={'execution_id': {'S': original['execution_id']}}, ConsistentRead=True)['Item']
            link.release.set()
            assert link.applied.wait(8), 'delayed AWS request did not return'
            evidence['at_arrival'] = deployment.item(event)
            link.deliver.set()
            evidence['retired_result'] = worker.result()
            evidence['retired_entered'] = worker.entered
            evidence['after'] = deployment.item(event)
            evidence['registry'] = deployment.db.get_item(
                TableName=deployment.tables['EXECUTION_TABLE'],
                Key={'execution_id': {'S': original['execution_id']}}, ConsistentRead=True)['Item']
            if operation != 'acquire' and not successor_runs:
                rival = deployment.request(event, wait_s=0)
                evidence['rival_result'] = deployment.invoke(rival)
            if not successor_runs:
                evidence['successor_result'] = deployment.invoke(successor)
            evidence['final'] = deployment.item(event)
            evidence['effects'] = deployment.effects(event)
            evidence['link_errors'] = link.errors[:]
            evidence['wire_request'] = link.observation
        finally:
            worker.close()
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    (LOG_ROOT / ('replacement-' + operation + '-' + str(successor_runs) + '.json')).write_text(
        json.dumps(evidence, indent=2))
    return evidence


def test_retired_worker_cannot_publish(replacement):
    evidence = replacement
    result = evidence['retired_result']
    if evidence['operation'] == 'delete':
        assert result.get('error') == 'ValueError', result
    elif result.get('status') == 'SUCCEEDED':
        assert evidence['successor_runs'] and result.get('result') == evidence['successor']['direct_result'], evidence
    if evidence['operation'] == 'acquire':
        assert not evidence['retired_entered'], evidence


def test_retired_worker_preserves_order(replacement):
    evidence = replacement
    expected = evidence.get('committed', evidence['before'])
    # A refused caller may conservatively reserve its still-live execution's
    # order before discovering revocation. That is safe if it never enters the
    # handler and the current invocation can continue. Terminal publication and
    # deletion, however, may not be repaired after becoming visible to readers.
    snapshots = [evidence['after']]
    if evidence['operation'] != 'acquire':
        snapshots.append(evidence['at_arrival'])
    for item in snapshots:
        if evidence['operation'] == 'acquire':
            if item:
                assert item.get('status', {}).get('S') == 'INPROGRESS', evidence
                assert item.get('owner_execution_id', {}).get('S') == evidence['request']['execution_id'], evidence
        else:
            for field in ('status', 'owner_execution_id'):
                assert item.get(field) == expected.get(field), evidence
            if expected.get('status', {}).get('S') == 'INPROGRESS':
                assert Decimal(item['in_progress_expiration']['N']) == Decimal(expected['in_progress_expiration']['N']), evidence
            if expected.get('status', {}).get('S') == 'COMPLETED':
                assert json.loads(item['response_data']['S']) == json.loads(expected['response_data']['S']), evidence
    assert evidence['registry'] == evidence['registry_before_release'], evidence
    if expected.get('status', {}).get('S') == 'COMPLETED':
        assert json.loads(evidence['after']['response_data']['S']) == json.loads(expected['response_data']['S'])
    elif expected:
        rival = evidence['rival_result']
        assert rival.get('status') == 'FAILED' and rival.get('error') == 'AlreadyInProgress', evidence


def test_replacement_continues_and_caches(replacement):
    evidence = replacement
    result = evidence['successor_result']
    assert result.get('status') == 'SUCCEEDED', evidence
    assert result.get('result') == evidence['successor']['direct_result'], evidence
    assert evidence['final'].get('owner_execution_id', {}).get('S') == evidence['request']['execution_id'], evidence
    assert json.loads(evidence['final']['response_data']['S']) == result['result'], evidence
    assert not evidence['effects'], evidence
    assert not evidence['link_errors'], evidence
