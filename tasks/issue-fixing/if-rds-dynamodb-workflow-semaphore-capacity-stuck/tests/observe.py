import json
import threading
import time
from decimal import Decimal
from pathlib import Path
import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import ClientError
from branches import Branches


def scan(table):
    items, args = [], {'ConsistentRead': True}
    while True:
        page = table.scan(**args)
        items.extend(page['Items'])
        if not page.get('LastEvaluatedKey'):
            return {w['wf_key']: w for w in items if not w['wf_key'].startswith('@run/')}
        args['ExclusiveStartKey'] = page['LastEvaluatedKey']


def change(table, item, changes):
    names = {'#v': 'version', '#p': 'phase', '#u': 'uid'}
    values = {':v': item['version'], ':next': item['version'] + 1, ':p': item['phase'], ':u': item['uid']}
    fields = ['#v=:next']
    for i, (key, value) in enumerate(changes.items()):
        names[f'#n{i}'] = key
        values[f':n{i}'] = value
        fields.append(f'#n{i}=:n{i}')
    table.update_item(Key={'wf_key': item['wf_key']}, UpdateExpression='SET ' + ','.join(fields),
                      ConditionExpression='#v=:v AND #p=:p AND #u=:u',
                      ExpressionAttributeNames=names, ExpressionAttributeValues=values)


class Monitor:
    def __init__(self, dsn, endpoint, tables, ledger, limits):
        resource = boto3.resource('dynamodb', endpoint_url=endpoint, region_name='us-east-1',
                                  config=Config(connect_timeout=2, read_timeout=5, retries={'max_attempts': 1}))
        self.tables = {c: resource.Table(t) for c, t in tables.items()}
        self.ledger = resource.Table(ledger)
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.limits = limits
        self.rows = set()
        self.heartbeats = {}
        self.items = {c: {} for c in tables}
        self.executions = {}
        self.deliveries = {}
        self.launch_delay = {}
        self.token_rows = set()
        self.violations = set()
        self.protected_pending = set()
        self.protected_held = set()
        self.seen = set()
        self.removed = {}
        self.missing = {}
        self.history = []
        self.branches = Branches(self)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while not self.stop.is_set():
                self.sample()
                self.stop.wait(.35)
        except Exception as exc:
            self.violations.add('observer error: ' + repr(exc))

    def sample(self):
        # SQL precedes lifecycle reads: a legitimate completion releases only after terminal status.
        token_rows = set(self.conn.execute('SELECT name,workflowkey,controller,held,execution_id FROM sync_state').fetchall())
        rows = {r[:4] for r in token_rows}
        beats = dict(self.conn.execute('SELECT controller,time FROM sync_controller').fetchall())
        items = {c: scan(table) for c, table in self.tables.items()}
        now = time.monotonic()
        wall = Decimal(str(time.time()))
        for row in self.protected_held.copy():
            name, key, controller = row
            item = items[controller].get(key)
            if item and item['phase'] not in {'Succeeded', 'Failed', 'Error'} and (*row, True) not in rows:
                self.violations.add(f'protected held row disappeared: {row}')
        for row in self.protected_pending.copy():
            if (*row, True) in rows:
                self.protected_pending.discard(row)
            elif (*row, False) not in rows:
                self.violations.add(f'pending row disappeared before grant: {row}')
        for name, limit in self.limits.items():
            if sum(1 for n, _, _, held in rows if n == 'sem/' + name and held) > limit:
                self.violations.add('SQL capacity exceeded: ' + name)
        for controller, workflows in items.items():
            for key, item in workflows.items():
                identity = (controller, key, item['uid'])
                row = ('sem/' + item['semaphore'], key, controller, True, item['uid'])
                if (identity not in self.deliveries and item['phase'] == 'Running'
                        and item['semaphore'] in item.get('sync_holding', []) and row in token_rows):
                    # The external launch queue stores the eligible request. Workflow
                    # deletion cannot retract it, and it may arrive much later.
                    self.deliveries[identity] = {'item': item, 'at': now + self.launch_delay.get(item['uid'], 0),
                                                  'delivered': False}
        for identity, delivery in list(self.deliveries.items()):
            if delivery['delivered'] or now < delivery['at']:
                continue
            delivery['delivered'] = True
            controller, key, uid = identity
            item = delivery['item']
            try:
                self.tables[controller].update_item(Key={'wf_key': '@run/' + uid},
                    UpdateExpression='SET #started=:yes',
                    ConditionExpression='(attribute_not_exists(#closed) OR #closed=:no) AND attribute_not_exists(#started)',
                    ExpressionAttributeNames={'#started': 'started', '#closed': 'closed'},
                    ExpressionAttributeValues={':yes': True, ':no': False})
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
                continue
            self.executions[identity] = {'start': now, 'end': None, 'duration': float(item['run_seconds']),
                                        'semaphore': item['semaphore'], 'uid': uid}
            self.ledger.put_item(Item={'exec_id': uid, 'start_ts': wall,
                                      'semaphore': item['semaphore'], 'controller': controller, 'wf_key': key})
        for (controller, key, uid), execution in list(self.executions.items()):
            if execution['end'] is not None:
                continue
            item = items[controller].get(key)
            row = ('sem/' + execution['semaphore'], key, controller, True, uid)
            identity = (controller, key, uid)
            if now < execution['start'] + execution['duration']:
                if row not in token_rows:
                    self.missing[identity] = self.missing.get(identity, 0) + 1
                    if self.missing[identity] == 2:
                        self.violations.add('execution lost SQL hold: ' + repr(identity))
                        self.ledger.update_item(Key={'exec_id': uid},
                                                 UpdateExpression='SET row_lost_ts=:t', ExpressionAttributeValues={':t': wall})
                else:
                    self.missing[identity] = 0
            else:
                execution['end'] = now
                self.ledger.update_item(Key={'exec_id': uid},
                                         UpdateExpression='SET end_ts=:t', ExpressionAttributeValues={':t': wall})
                self.tables[controller].update_item(Key={'wf_key': '@run/' + uid},
                    UpdateExpression='SET #done=:yes', ExpressionAttributeNames={'#done': 'done'},
                    ExpressionAttributeValues={':yes': True})
            # Completion notification may race a controller write; retry without
            # claiming that the physical execution is still running.
        self.branches.sample(now, token_rows)
        for (controller, key, uid), execution in list(self.executions.items()):
            item = items[controller].get(key)
            if (execution['end'] is not None and self.branches.complete(uid) and item and item['uid'] == uid
                    and item['phase'] == 'Running' and not item.get('exec_done')):
                try:
                    change(self.tables[controller], item, {'exec_done': True})
                except ClientError as exc:
                    if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                        raise
        for name, limit in self.limits.items():
            active = sum(1 for (c,k,u), e in self.executions.items() if e['semaphore'] == name
                         and (e['end'] is None or self.branches.unfinished(u)))
            if active > limit:
                self.violations.add('execution overlap exceeded capacity: ' + name)
        for old in self.rows - rows:
            self.removed.setdefault(old, now)
        self.rows, self.items, self.heartbeats = rows, items, beats
        self.token_rows = token_rows
        self.seen.update(rows)
        self.history.append({'time': now, 'rows': sorted(token_rows), 'phases': {c: {k: v['phase'] for k,v in w.items()} for c,w in items.items()}})

    def started(self, controller, key, uid=None):
        return next((e['start'] for (c,k,u),e in self.executions.items()
                     if c == controller and k == key and (uid is None or uid == u)), None)

    def ended(self, controller, key, uid=None):
        return next((e['end'] for (c,k,u),e in self.executions.items()
                     if c == controller and k == key and (uid is None or uid == u)), None)

    def close(self):
        self.stop.set()
        self.thread.join(15)
        Path('/logs/verifier/observations.json').write_text(json.dumps({'violations': sorted(self.violations),
             'executions': {repr(k): v for k,v in self.executions.items()},
             'nodes': self.branches.nodes, 'registration_denied': sorted(self.branches.registration_denied),
             'samples': self.history}, default=str))
        self.conn.close()
