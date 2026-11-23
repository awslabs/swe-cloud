"""Independent deployment, side-effect adapter and execution-history observer."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import boto3
from botocore.config import Config
from gateway import Gateway
from cdk_seed.cfn_deploy import deploy

APP = '/app'
BASE = 'http://localhost:4566'
PROXY = 'http://localhost:4567'


def client(service, endpoint=BASE, secret=None):
    c = boto3.client(service, region_name='us-east-1', endpoint_url=endpoint,
                     aws_access_key_id='test', aws_secret_access_key='test',
                     config=Config(retries={'max_attempts': 0}, read_timeout=60))
    if secret:
        def header(params, **kwargs):
            params['headers']['X-Driver-Key'] = secret
        c.meta.events.register('before-call.*.*', header)
    return c


def eventually(predicate, timeout=12, description='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('timed out waiting for ' + description)


def asl(queues):
    def task(queue, following, result=None):
        return {'Type': 'Task', 'Resource': 'arn:aws:states:::sqs:sendMessage.waitForTaskToken',
                'Parameters': {'QueueUrl': queues[queue], 'MessageBody': {
                    'entity.$': '$.entity', 'version.$': '$.version', 'taskToken.$': '$$.Task.Token',
                    'executionArn.$': '$$.Execution.Id'}},
                'TimeoutSeconds': 120, 'ResultPath': result, 'Next': following}
    states = {
        'route': {'Type': 'Choice', 'Choices': [{'Variable': '$.version', 'NumericEquals': 1, 'Next': 'sync_data'}],
                  'Default': 'wait_prev_command'},
        'wait_prev_command': task('wait-prev', 'dup_check', '$.wait'),
        'dup_check': {'Type': 'Choice', 'Choices': [{'Variable': '$.wait.result', 'StringEquals': 'duplicate',
                                                    'Next': 'duplicate_done'}], 'Default': 'sync_data'},
        'sync_data': task('sync', 'finish'), 'finish': task('finish', 'done'),
        'done': {'Type': 'Succeed'}, 'duplicate_done': {'Type': 'Succeed'},
        'wait_prev_command_timeout': {'Type': 'Pass', 'Next': 'failed'}, 'failed': {'Type': 'Fail'}}
    states['wait_prev_command']['Catch'] = [{'ErrorEquals': ['States.Timeout'], 'Next': 'wait_prev_command_timeout'}]
    return {'StartAt': 'route', 'States': states}


class Rig:
    def __init__(self, logdir, failed_entity=None):
        self.logdir = Path(logdir)
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.ident = uuid.uuid4().hex[:12]
        self.ddb, self.sqs, self.sfn, self.iam = [client(s) for s in ('dynamodb', 'sqs', 'stepfunctions', 'iam')]
        self.table = 'commands-' + self.ident
        self.effects_table, self.projection_table = 'effects-' + self.ident, 'projection-' + self.ident
        self.executions, self.processes, self.files = {}, [], []
        self.gates = []
        self.gateway = None
        self.sink_process = None
        outputs = deploy(client('cloudformation'), 'seed-command-' + self.ident,
                         Path(__file__).parent / 'cdk_seed/seed.template.json', Ident=self.ident)
        self.queues = {'wait-prev': outputs['WaitQueue'], 'sync': outputs['SyncQueue'],
                       'finish': outputs['FinishQueue']}
        self.machine = outputs['MachineArn']
        self.gateway = Gateway(self.ddb, self.sfn, self.table, self.queues['wait-prev'])
        self.publisher = client('dynamodb', PROXY, self.gateway.secret)
        self.env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': APP,
                    'AWS_ENDPOINT_URL': PROXY, 'AWS_REGION': 'us-east-1', 'AWS_DEFAULT_REGION': 'us-east-1',
                    'AWS_ACCESS_KEY_ID': 'test', 'AWS_SECRET_ACCESS_KEY': 'test',
                    'COMMANDS_TABLE': self.table, 'WAIT_QUEUE_URL': self.queues['wait-prev'],
                    'FINISH_QUEUE_URL': self.queues['finish'], 'SYNC_QUEUE_URL': self.queues['sync']}
        for mode, count in [('wait-worker', 2), ('finish-worker', 3)]:
            for i in range(count):
                self.spawn([sys.executable, str(Path(APP) / 'solution'), mode], self.env, mode + str(i))
        sink_env = {**self.env, 'DRIVER_SECRET': self.gateway.secret,
                    'EFFECTS_TABLE': self.effects_table, 'PROJECTION_TABLE': self.projection_table,
                    'FAILED_ENTITY': failed_entity or ''}
        self.sink_process = self.spawn([sys.executable, str(Path(__file__).resolve()), 'sink'], sink_env, 'sink')

    def spawn(self, args, env, name):
        output = open(self.logdir / (name + '.log'), 'w')
        self.files.append(output)
        process = subprocess.Popen(args, env=env, cwd=tempfile.mkdtemp(prefix='command-role-'),
                                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(process)
        return process

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def publish(self, entity, version, duplicate=False):
        if not duplicate:
            self.publisher.update_item(TableName=self.table, Key=self.key(entity, version),
                                       UpdateExpression='SET #s = if_not_exists(#s, :s)',
                                       ExpressionAttributeNames={'#s': 'status'},
                                       ExpressionAttributeValues={':s': {'S': 'created'}})
        execution = self.sfn.start_execution(stateMachineArn=self.machine, name=uuid.uuid4().hex,
                                              input=json.dumps({'entity': entity, 'version': version}))['executionArn']
        self.executions[execution] = (entity, version)
        return execution

    @staticmethod
    def key(entity, version):
        return {'pk': {'S': entity}, 'sk': {'N': str(version)}}

    def row(self, entity, version):
        return self.ddb.get_item(TableName=self.table, Key=self.key(entity, version), ConsistentRead=True).get('Item', {})

    def state(self, execution):
        return self.sfn.describe_execution(executionArn=execution)['status']

    def history(self, execution):
        events, token = [], None
        while True:
            args = {'executionArn': execution}
            if token:
                args['nextToken'] = token
            response = self.sfn.get_execution_history(**args)
            events.extend(response['events'])
            token = response.get('nextToken')
            if not token:
                return events

    def wait_release(self, execution):
        state = None
        for event in self.history(execution):
            if event['type'] == 'TaskStateEntered':
                state = event['stateEnteredEventDetails']['name']
            if event['type'] == 'TaskSucceeded' and state == 'wait_prev_command':
                output = json.loads(event['taskSucceededEventDetails']['output'])
                if output.get('result') != 'duplicate':
                    return event['timestamp'].timestamp()
        return None

    def wait_deleted(self, entity, count=1):
        return sum(1 for _, e in self.gateway.deleted if e['entity'] == entity) >= count

    def successful(self, *executions, timeout=15):
        eventually(lambda: all(self.state(e) == 'SUCCEEDED' for e in executions), timeout, 'successful executions')

    def effects(self, entity):
        return self.ddb.query(TableName=self.effects_table, KeyConditionExpression='pk = :p',
                              ExpressionAttributeValues={':p': {'S': entity}}, ConsistentRead=True)['Items']

    def deadline(self, execution, start, seconds=8):
        released = eventually(lambda: self.wait_release(execution), seconds + .5, 'wait callback')
        assert released - start <= seconds, (released, start, 'callback exceeded deployment budget')

    def assert_outcomes(self, entity, versions, allow_parked=()):
        effects = self.effects(entity)
        for version in versions:
            matching = [e for e in effects if int(e['version']['N']) == version]
            assert len(matching) == 1, (entity, version, 'effects', matching)
            assert self.row(entity, version).get('status', {}).get('S') == 'finish:FINISHED'
            if version > 1:
                assert matching[0]['predStatusAtSync']['S'] in ('finish:STARTED', 'finish:FINISHED'), matching
        assert len(effects) == len(versions), (entity, 'unexpected effects', effects)
        projection = self.ddb.get_item(TableName=self.projection_table, Key={'pk': {'S': entity}},
                                        ConsistentRead=True)['Item']
        assert int(projection['version']['N']) == max(versions), projection
        for execution, (ent, version) in self.executions.items():
            if ent != entity:
                continue
            history = self.history(execution)
            syncs = [e for e in history if e['type'] == 'TaskStateEntered' and
                     e['stateEnteredEventDetails']['name'] == 'sync_data']
            if execution not in allow_parked:
                assert self.state(execution) == 'SUCCEEDED', (execution, self.state(execution))
            if version > 1 and syncs:
                released = self.wait_release(execution)
                commits = [t for t, k, row in self.gateway.commits if k == (entity, version - 1) and
                           row.get('status', {}).get('S') == 'finish:STARTED']
                assert commits and released is not None, (execution, 'missing release evidence')
                # History is millisecond precision; commit observation is the HTTP response.
                assert released >= min(commits) - .005, (execution, 'released before predecessor entered finish')
        for version in versions:
            entries = sum(1 for execution, pair in self.executions.items() if pair == (entity, version)
                          for e in self.history(execution) if e['type'] == 'TaskStateEntered' and
                          e['stateEnteredEventDetails']['name'] == 'sync_data')
            assert entries == 1, (entity, version, 'sync entries', entries)

    def assert_health(self):
        def empty():
            for queue in self.queues.values():
                attributes = self.sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=[
                    'ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible'])['Attributes']
                if any(int(value) for value in attributes.values()):
                    return False
            return True
        # Queues use the documented 30 s visibility timeout. A finish message kept for one
        # ordinary redelivery (for example after exhausted successor reads) may take that
        # long to go; callback and wait-acknowledgement deadlines are asserted separately.
        eventually(empty, 40, 'drained step queues')
        assert not self.gateway.violations, self.gateway.violations
        assert all(p.poll() is None for p in self.processes), 'worker or sink process exited; see role logs'
        old = [(time.time() - start, event) for start, event in self.gateway.receipts.values()]
        assert not [r for r in old if r[0] > 3], ('unacknowledged waits', old)

    def close(self):
        for gate in self.gates:
            gate.set()
        # Make evidence survive assertion failures, including incomplete executions.
        evidence = {'commits': self.gateway.commits, 'reads': self.gateway.read_log,
                    'callbacks': self.gateway.callbacks, 'violations': self.gateway.violations,
                    'executions': {}}
        for execution in list(self.executions):
            try:
                evidence['executions'][execution] = {'status': self.state(execution), 'history': self.history(execution)}
                if self.state(execution) == 'RUNNING':
                    self.sfn.stop_execution(executionArn=execution)
            except Exception as exc:
                evidence['executions'][execution] = repr(exc)
        (self.logdir / 'evidence.json').write_text(json.dumps(evidence, default=str, indent=2))
        for process in self.processes:
            process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for file in self.files:
            file.close()
        self.gateway.close()


def sink():
    table = os.environ['COMMANDS_TABLE']
    effects, projection = os.environ['EFFECTS_TABLE'], os.environ['PROJECTION_TABLE']
    queue = os.environ['SYNC_QUEUE_URL']
    secret = os.environ['DRIVER_SECRET']
    ddb, sqs, sfn = client('dynamodb'), client('sqs'), client('stepfunctions', PROXY, secret)
    writer = client('dynamodb', PROXY, secret)

    def apply(message):
        event = json.loads(message['Body'])
        entity, version, execution = event['entity'], event['version'], event['executionArn']
        key = Rig.key(entity, version)
        def status(value):
            writer.update_item(TableName=table, Key=key, UpdateExpression='SET #s = :s',
                                ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':s': {'S': value}})
        status('sync_data:STARTED')
        if entity == os.environ.get('FAILED_ENTITY') and version == 1:
            status('sync_data:FAILED')
            client('stepfunctions').stop_execution(executionArn=execution, error='DownstreamRejected')
        else:
            pred = 'none'
            if version > 1:
                pred = ddb.get_item(TableName=table, Key=Rig.key(entity, version - 1), ConsistentRead=True).get(
                    'Item', {}).get('status', {}).get('S', 'missing')
            ddb.put_item(TableName=effects, Item={'pk': {'S': entity}, 'sk': {'S': str(version) + '#' + execution},
                         'version': {'N': str(version)}, 'executionArn': {'S': execution}, 'predStatusAtSync': {'S': pred}})
            ddb.put_item(TableName=projection, Item={'pk': {'S': entity}, 'version': {'N': str(version)}})
            status('sync_data:FINISHED')
            sfn.send_task_success(taskToken=event['taskToken'], output='{"result":"ok"}')
        sqs.delete_message(QueueUrl=queue, ReceiptHandle=message['ReceiptHandle'])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        pending = set()
        while True:
            for future in list(pending):
                if future.done():
                    future.result()
                    pending.remove(future)
            for message in sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10,
                                                WaitTimeSeconds=1).get('Messages', []):
                pending.add(executor.submit(apply, message))


if __name__ == '__main__':
    sink()
