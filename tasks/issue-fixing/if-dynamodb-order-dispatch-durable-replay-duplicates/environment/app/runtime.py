import json
import os
import time
from aws import client


class Suspended(BaseException):
    def __init__(self, resume_at_ms):
        self.resume_at_ms = resume_at_ms


class Context:
    def __init__(self, request):
        self.execution_id = request['execution_id']
        self.invocation_id = request['invocation_id']
        self.executions = os.environ.get('EXECUTION_TABLE', 'durable_executions')
        self.execution_start_ms = request['execution_start_ms']
        self.execution_timeout_ms = request['execution_timeout_ms']
        self.invocation_deadline = time.monotonic() + request.get('invocation_timeout_s', 4)
        self.db = client()
        self.table = os.environ.get('CHECKPOINT_TABLE', 'durable_checkpoints')
        self.cursor = 0
        items = self.db.query(TableName=self.table,
                              KeyConditionExpression='execution_id = :id',
                              ExpressionAttributeValues={':id': {'S': self.execution_id}},
                              ConsistentRead=True)['Items']
        self.history = {int(item['seq']['N']): item for item in items}
        self.operations_count = 1 + len(items)
        self.mode = 'REPLAY' if items else 'EXECUTION'

    def get_remaining_time_in_millis(self):
        return max(0, int((self.invocation_deadline - time.monotonic()) * 1000))

    def _assert_active(self):
        row = self.db.get_item(TableName=self.executions,
                               Key={'execution_id': {'S': self.execution_id}},
                               ConsistentRead=True).get('Item', {})
        if row.get('active_invocation_id', {}).get('S') != self.invocation_id:
            raise RuntimeError('Invocation replaced')

    def _operation(self, kind, name):
        self._assert_active()
        self.cursor += 1
        item = self.history.get(self.cursor)
        if item and (item['kind']['S'] != kind or item['name']['S'] != name):
            raise ValueError('Checkpoint history does not match handler')
        return item

    def _save(self, kind, name, **fields):
        self.db.put_item(TableName=self.table,
                         Item={'execution_id': {'S': self.execution_id},
                               'seq': {'N': str(self.cursor)}, 'kind': {'S': kind},
                               'name': {'S': name}, **fields},
                         ConditionExpression='attribute_not_exists(seq)')

    def step(self, name, fn):
        item = self._operation('step', name)
        if item:
            return json.loads(item['data']['S'])
        result = fn()
        self._save('step', name, data={'S': json.dumps(result)})
        return result

    def wait(self, seconds):
        item = self._operation('wait', 'warehouse')
        if item:
            at = int(item['resume_at_ms']['N'])
            if int(time.time() * 1000) < at:
                raise Suspended(at)
            return
        at = int(time.time() * 1000 + seconds * 1000)
        self._save('wait', 'warehouse', resume_at_ms={'N': str(at)})
        raise Suspended(at)
