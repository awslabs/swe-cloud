"""Independent external DAG executor. No application imports."""
import time
import uuid
from botocore.exceptions import ClientError


class Branches:
    def __init__(self, monitor):
        self.monitor = monitor
        self.plans = {}
        self.nodes = {}
        self.registration_denied = set()
        self.missing = {}

    def plan(self, uid, specs):
        ids = {spec['tag']: uuid.uuid4().hex for spec in specs}
        self.plans[uid] = [dict(spec, node=ids[spec['tag']],
                                parent=uid if spec['parent'] == 'root' else ids[spec['parent']],
                                registered=False, attempted=False, delivered=False,
                                start=None, end=None) for spec in specs]
        return ids

    def sample(self, now, token_rows):
        m = self.monitor
        for identity, execution in list(m.executions.items()):
            controller, key, uid = identity
            if uid not in self.plans:
                continue
            table = m.tables[controller]
            for spec in self.plans[uid]:
                node = spec['node']
                self.nodes[node] = dict(spec, controller=controller, key=key, uid=uid,
                                        semaphore=execution['semaphore'])
                if not spec['attempted'] and now >= execution['start'] + spec['register']:
                    parent = table.get_item(Key={'wf_key': '@run/' + spec['parent']},
                                            ConsistentRead=True).get('Item', {})
                    # A child publisher only exists after its parent starts.
                    if not parent.get('started'):
                        continue
                    spec['attempted'] = True
                    try:
                        table.update_item(Key={'wf_key': '@run/' + spec['parent']},
                            UpdateExpression='ADD #children :child',
                            ConditionExpression='(attribute_not_exists(#closed) OR #closed=:no) AND #started=:yes',
                            ExpressionAttributeNames={'#children': 'children', '#closed': 'closed', '#started': 'started'},
                            ExpressionAttributeValues={':child': {node}, ':no': False, ':yes': True})
                        spec['registered'] = True
                    except ClientError as exc:
                        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                            raise
                        self.registration_denied.add(node)
                if spec['registered'] and not spec['delivered'] and now >= execution['start'] + spec['deliver']:
                    spec['delivered'] = True
                    try:
                        table.update_item(Key={'wf_key': '@run/' + node}, UpdateExpression='SET #started=:yes',
                            ConditionExpression='(attribute_not_exists(#closed) OR #closed=:no) AND attribute_not_exists(#started)',
                            ExpressionAttributeNames={'#started': 'started', '#closed': 'closed'},
                            ExpressionAttributeValues={':yes': True, ':no': False})
                        spec['start'] = now
                        m.ledger.put_item(Item={'exec_id': node, 'controller': controller, 'wf_key': key,
                                               'root_uid': uid, 'semaphore': execution['semaphore'],
                                               'start_ts': str(time.time())})
                    except ClientError as exc:
                        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                            raise
                if spec['start'] is not None and spec['end'] is None:
                    if now >= spec['start'] + spec['duration']:
                        table.update_item(Key={'wf_key': '@run/' + node}, UpdateExpression='SET #done=:yes',
                            ExpressionAttributeNames={'#done': 'done'}, ExpressionAttributeValues={':yes': True})
                        spec['end'] = now
                        m.ledger.update_item(Key={'exec_id': node}, UpdateExpression='SET end_ts=:t',
                                            ExpressionAttributeValues={':t': str(time.time())})
                    else:
                        row = ('sem/' + execution['semaphore'], key, controller, True, uid)
                        self.missing[node] = self.missing.get(node, 0) + 1 if row not in token_rows else 0
                        if self.missing[node] >= 2:
                            m.violations.add('descendant execution lost SQL hold: ' + node)
                self.nodes[node] = dict(spec, controller=controller, key=key, uid=uid,
                                        semaphore=execution['semaphore'])

    def unfinished(self, uid):
        return any(spec['start'] is not None and spec['end'] is None for spec in self.plans.get(uid, []))

    def complete(self, uid):
        # Normal completion includes publication retries, queued deliveries and
        # all descendants. Cancellation deliberately receives no notification.
        return all(spec['attempted'] and (not spec['registered'] or
                   (spec['delivered'] and (spec['start'] is None or spec['end'] is not None)))
                   for spec in self.plans.get(uid, []))
