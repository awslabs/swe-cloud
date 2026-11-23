"""Private AWS wire adapter: real Floci data plane, missing AWS semantics restored."""
import collections
import copy
import http.client
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Gateway:
    def __init__(self, ddb, sfn, table, wait_queue):
        self.ddb, self.sfn, self.table, self.wait_queue = ddb, sfn, table, wait_queue
        self.secret = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.row_locks = collections.defaultdict(threading.RLock)
        self.token_lock = threading.Lock()
        self.snapshots = collections.defaultdict(list)
        self.commits, self.callbacks, self.read_log, self.violations = [], [], [], []
        self.tokens, self.completed, self.receipts, self.deleted = {}, set(), {}, []
        self.token_events = {}
        self.reads, self.failures = collections.Counter(), collections.Counter()
        self.before_write = lambda key, status, body: None
        self.after_write = lambda key, row, body: None
        self.before_read = lambda key: None
        self.after_read = lambda key, row: None
        self.before_send = lambda body: None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                try:
                    body = json.loads(raw)
                    code, payload = owner.request(self.path, dict(self.headers), body)
                except Exception as exc:
                    owner.violations.append('gateway: ' + repr(exc))
                    code, payload = 500, {'__type': 'InternalFailure', 'message': str(exc)}
                encoded = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/x-amz-json-1.0')
                if code >= 400:
                    self.send_header('x-amzn-ErrorType', payload.get('__type', 'InternalFailure'))
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # consumers may terminate while a receive is outstanding
        self.server = ThreadingHTTPServer(('127.0.0.1', 4567), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def key(item):
        if 'pk' in item and 'sk' in item:
            return item['pk']['S'], int(item['sk']['N'])

    @staticmethod
    def assignment(body, attribute):
        if 'Item' in body:
            return body['Item'].get(attribute, {}).get('S')
        names = body.get('ExpressionAttributeNames', {})
        values = body.get('ExpressionAttributeValues', {})
        expression = body.get('UpdateExpression', '')
        for lhs, rhs in re.findall(r'([#\w]+)\s*=\s*(:\w+)', expression):
            if names.get(lhs, lhs) == attribute:
                return values.get(rhs, {}).get('S')

    @classmethod
    def status(cls, body):
        return cls.assignment(body, 'status')

    @staticmethod
    def forward(path, headers, body):
        connection = http.client.HTTPConnection('localhost', 4566, timeout=60)
        headers = {k: v for k, v in headers.items() if k.lower() not in
                   ('host', 'content-length', 'connection', 'accept-encoding')}
        connection.request('POST', path, body=json.dumps(body).encode(), headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data or b'{}')

    def error(self, code, message):
        return 400, {'__type': code, 'message': message}

    def stale(self, key):
        cutoff = time.time() - 3
        with self.lock:
            items = [item for when, item in self.snapshots[key] if when <= cutoff]
            return copy.deepcopy(items[-1]) if items else {}

    def row(self, key):
        return self.ddb.get_item(TableName=self.table, Key={'pk': {'S': key[0]}, 'sk': {'N': str(key[1])}},
                                 ConsistentRead=True).get('Item', {})

    def request(self, path, headers, body):
        headers = {k.lower(): v for k, v in headers.items()}
        operation = headers.get('x-amz-target', '').split('.')[-1]
        trusted = headers.get('x-driver-key') == self.secret
        table = body.get('TableName')
        if not trusted:
            if operation in ('SendMessage', 'SendMessageBatch', 'ChangeMessageVisibility', 'ChangeMessageVisibilityBatch',
                             'CreateQueue', 'SetQueueAttributes', 'PurgeQueue', 'DeleteQueue', 'StartExecution',
                             'StopExecution', 'CreateStateMachine', 'UpdateStateMachine', 'DeleteItem', 'DeleteTable', 'CreateTable'):
                self.violations.append('worker used deployment operation ' + operation)
                return self.error('AccessDeniedException', 'deployment-owned operation')
            if table and table != self.table:
                self.violations.append('worker accessed deployment table ' + table)
                return self.error('AccessDeniedException', 'deployment-owned table')

        if operation == 'SendTaskSuccess':
            self.before_send(body)
            # Floci accepts repeats; serialize acceptance across *all* clients.
            with self.token_lock:
                token = body['taskToken']
                if token in self.completed:
                    self.callbacks.append((time.time(), token, body, 'TaskDoesNotExist'))
                    return self.error('TaskDoesNotExist', 'callback already closed')
                event = self.token_events.get(token)
                if event and self.sfn.describe_execution(executionArn=event['executionArn'])['status'] != 'RUNNING':
                    self.callbacks.append((time.time(), token, body, 'TaskTimedOut'))
                    return self.error('TaskTimedOut', 'execution is no longer running')
                code, result = self.forward(path, headers, body)
                if code == 200:
                    self.completed.add(token)
                self.callbacks.append((time.time(), token, body, 'ok' if code == 200 else 'error'))
                return code, result

        writes = []
        if operation in ('UpdateItem', 'PutItem') and table == self.table:
            writes.append((self.key(body.get('Key', body.get('Item', {}))), body))
        if operation == 'TransactWriteItems':
            for action in body.get('TransactItems', []):
                op, request = next(iter(action.items()))
                if request.get('TableName') == self.table and op in ('Put', 'Update'):
                    writes.append((self.key(request.get('Key', request.get('Item', {}))), request))
        for key, request in writes:
            self.before_write(key, self.status(request), request)

        read_keys = []
        consistent = True
        if operation == 'GetItem' and table == self.table:
            read_keys = [self.key(body['Key'])]
            consistent = body.get('ConsistentRead', False)
        elif operation == 'BatchGetItem' and self.table in body.get('RequestItems', {}):
            request = body['RequestItems'][self.table]
            read_keys = [self.key(k) for k in request['Keys']]
            consistent = request.get('ConsistentRead', False)
        elif operation == 'TransactGetItems':
            read_keys = [self.key(x['Get']['Key']) for x in body['TransactItems'] if x['Get']['TableName'] == self.table]
        elif operation in ('Query', 'Scan') and table == self.table:
            # Enumerate the actual selection, then apply the lagged view.
            direct = getattr(self.ddb, operation.lower())(**body)
            read_keys = [self.key(x) for x in direct.get('Items', [])]
            consistent = body.get('ConsistentRead', False)

        for key in read_keys:
            self.before_read(key)
            if not trusted:
                with self.lock:
                    self.reads[key] += 1
                    if self.reads[key] > 20:
                        self.violations.append('read budget exceeded: ' + str(key))
                        return self.error('ProvisionedThroughputExceededException', 'read budget')
                    if self.failures[key]:
                        self.failures[key] -= 1
                        return self.error('ProvisionedThroughputExceededException', 'transient capacity')

        # Preserve a commit order and matching snapshot even for overlapping writes.
        locks = [self.row_locks[k] for k in sorted(set(k for k, _ in writes))]
        for lock in locks:
            lock.acquire()
        try:
            code, result = self.forward(path, headers, body)
            captured = []
            if code == 200:
                for key, request in writes:
                    when = time.time()
                    row = self.row(key)
                    with self.lock:
                        self.snapshots[key].append((when, copy.deepcopy(row)))
                        self.commits.append((when, key, copy.deepcopy(row)))
                        if 'taskToken' in row:
                            self.tokens.setdefault(row['taskToken']['S'], (when, key))
                    captured.append((key, row, request))
        finally:
            for lock in reversed(locks):
                lock.release()
        for key, row, request in captured:
            self.after_write(key, row, request)

        if code == 200 and read_keys:
            def view(key):
                if consistent or trusted:
                    with self.row_locks[key]:
                        return self.row(key)
                return self.stale(key)
            if operation == 'GetItem':
                row = view(read_keys[0])
                result = {'Item': row} if row else {}
            elif operation in ('Query', 'Scan'):
                rows = [view(key) for key in read_keys]
                result['Items'] = [r for r in rows if r]
                result['Count'] = len(result['Items'])
            elif operation == 'BatchGetItem':
                result['Responses'][self.table] = [r for k in read_keys if (r := view(k))]
            # TransactGetItems are strongly consistent; leave response as is.
            for key in read_keys:
                row = view(key)
                self.read_log.append((time.time(), key, consistent, row))
                self.after_read(key, row)

        if code == 200 and operation == 'ReceiveMessage':
            for message in result.get('Messages', []):
                event = json.loads(message['Body'])
                self.token_events[event['taskToken']] = event
        if code == 200 and operation == 'ReceiveMessage' and body.get('QueueUrl') == self.wait_queue:
            for message in result.get('Messages', []):
                event = json.loads(message['Body'])
                self.receipts[message['ReceiptHandle']] = (time.time(), event)
        if code == 200 and operation in ('DeleteMessage', 'DeleteMessageBatch'):
            receipts = [body['ReceiptHandle']] if operation == 'DeleteMessage' else [x['ReceiptHandle'] for x in body['Entries']]
            for receipt in receipts:
                if receipt in self.receipts:
                    start, event = self.receipts.pop(receipt)
                    if time.time() - start > 3.0:
                        self.violations.append('wait handler exceeded 3s: ' + str((event['entity'], event['version'])))
                    self.deleted.append((time.time(), event))
        return code, result
