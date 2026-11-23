"""Verifier-owned durable invocation; only the wrapper is loaded from the deployment."""
import hashlib
import json
import os
import socket
import sys
import time
import traceback
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from idempotency import idempotent


def client():
    return boto3.client('dynamodb', endpoint_url=os.environ['AWS_ENDPOINT_URL'], region_name='us-east-1',
                        aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(connect_timeout=2, read_timeout=3, retries={'max_attempts': 1}))


class Suspension(BaseException):
    pass


class Context:
    def __init__(self, request):
        self.execution_id = request['execution_id']
        self.invocation_id = request['invocation_id']
        self.execution_start_ms = request['execution_start_ms']
        self.execution_timeout_ms = request['execution_timeout_ms']
        self.until = time.monotonic() + request['invocation_timeout_s']
        self.db = client()
        self.table = os.environ['CHECKPOINT_TABLE']
        self.position = 0
        rows = self.db.query(TableName=self.table, KeyConditionExpression='execution_id = :e',
                             ExpressionAttributeValues={':e': {'S': self.execution_id}},
                             ConsistentRead=True)['Items']
        self.entries = {int(row['seq']['N']): row for row in rows}
        self.operations_count = len(rows) + 1
        self.mode = 'REPLAY' if rows else 'EXECUTION'

    def get_remaining_time_in_millis(self):
        return max(0, int((self.until - time.monotonic()) * 1000))

    def step(self, name, callback):
        self.position += 1
        previous = self.entries.get(self.position)
        if previous:
            if previous['kind']['S'] != 'step' or previous['name']['S'] != name:
                raise RuntimeError('Checkpoint divergence')
            return json.loads(previous['data']['S'])
        result = callback()
        self.write('step', name, data={'S': json.dumps(result)})
        return result

    def write(self, kind, name, **fields):
        self.db.put_item(TableName=self.table, Item={
            'execution_id': {'S': self.execution_id}, 'seq': {'N': str(self.position)},
            'kind': {'S': kind}, 'name': {'S': name}, **fields},
            ConditionExpression='attribute_not_exists(seq)')

    def wait(self, seconds):
        self.position += 1
        previous = self.entries.get(self.position)
        if previous:
            at = int(previous['resume_at_ms']['N'])
            if at <= time.time() * 1000:
                return
        else:
            at = int(time.time() * 1000 + seconds * 1000)
            self.write('wait', 'warehouse', resume_at_ms={'N': str(at)})
        raise Suspension(at)


def main():
    request = json.loads(sys.argv[1])
    event = request['event']
    key = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    ctx = Context(request)

    @idempotent
    def handler(event, ctx):
        gate = os.environ.get('SEAM_SOCKET')
        if gate:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.connect(gate)
                conn.sendall((json.dumps({'seam': 'handler_entry', 'execution_id': ctx.execution_id,
                                          'payload_key': key, 'mode': ctx.mode,
                                          'operations_count': ctx.operations_count}) + '\n').encode())
                conn.recv(64)
        callback_number = None
        if request.get('audit_callback'):
            observed = ctx.db.update_item(TableName=os.environ['EFFECTS_TABLE'],
                Key={'payload_key': {'S': key}, 'execution_id': {'S': ctx.execution_id}},
                UpdateExpression='ADD callback_count :one',
                ExpressionAttributeValues={':one': {'N': '1'}}, ReturnValues='UPDATED_NEW')
            callback_number = int(observed['Attributes']['callback_count']['N'])
        if request.get('callback_transport_error'):
            raise ClientError({'Error': {'Code': 'InternalServerError',
                                        'Message': 'callback upstream unavailable'}}, 'Dispatch')
        if 'direct_result' in request:
            result = dict(request['direct_result'])
            if callback_number is not None:
                result['callback_number'] = callback_number
            return result
        if request.get('handler_error'):
            raise ValueError('dispatch unavailable')

        def dispatch():
            ctx.db.put_item(TableName=os.environ['EFFECTS_TABLE'], Item={
                'payload_key': {'S': key}, 'execution_id': {'S': ctx.execution_id},
                'dispatched_ms': {'N': str(int(time.time() * 1000))}})
            return {'dispatch': ctx.execution_id}

        dispatched = ctx.step('dispatch', dispatch)
        ctx.wait(request['wait_s'])
        return ctx.step('receipt', lambda: {'order': event, **dispatched, 'receipt': 'issued'})

    try:
        output = {'status': 'SUCCEEDED', 'result': handler(event, ctx)}
    except Suspension as suspension:
        output = {'status': 'SUSPENDED', 'resume_at_ms': suspension.args[0]}
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        output = {'status': 'FAILED', 'error': type(exc).__name__, 'detail': str(exc)}
    print(json.dumps(output), flush=True)


if __name__ == '__main__':
    main()
