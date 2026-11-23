import boto3
from botocore.config import Config


class Workflows:
    def __init__(self, endpoint, table):
        self.table = boto3.resource('dynamodb', endpoint_url=endpoint, region_name='us-east-1',
                                    config=Config(connect_timeout=2, read_timeout=5,
                                                  retries={'max_attempts': 1})).Table(table)

    def get(self, key):
        return self.table.get_item(Key={'wf_key': key}, ConsistentRead=True).get('Item')

    def list(self):
        items, args = [], {'ConsistentRead': True}
        while True:
            page = self.table.scan(**args)
            items.extend(w for w in page['Items'] if not w['wf_key'].startswith('@run/'))
            if not page.get('LastEvaluatedKey'):
                return items
            args['ExclusiveStartKey'] = page['LastEvaluatedKey']

    def update(self, item, changes):
        names = {'#v': 'version', '#p': 'phase', '#u': 'uid'}
        values = {':v': item['version'], ':next': item['version'] + 1, ':p': item['phase'], ':u': item['uid']}
        assignments = ['#v=:next']
        for i, (field, value) in enumerate(changes.items()):
            names[f'#f{i}'] = field
            values[f':f{i}'] = value
            assignments.append(f'#f{i}=:f{i}')
        self.table.update_item(Key={'wf_key': item['wf_key']}, UpdateExpression='SET ' + ','.join(assignments),
                               ConditionExpression='#v=:v AND #p=:p AND #u=:u',
                               ExpressionAttributeNames=names, ExpressionAttributeValues=values)

    def close_execution(self, uid):
        # Closing each node freezes that node's outgoing edges. Its returned
        # membership is then a durable frontier, including delayed launches.
        # Traverse all branches even if an ancestor is still executing.
        pending, seen, drained = [uid], set(), True
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            receipt = self.table.update_item(
                Key={'wf_key': '@run/' + node},
                UpdateExpression='SET #closed=:yes',
                ExpressionAttributeNames={'#closed': 'closed'},
                ExpressionAttributeValues={':yes': True}, ReturnValues='ALL_NEW')['Attributes']
            seen.add(node)
            pending.extend(receipt.get('children', set()) - seen)
            if receipt.get('started') and not receipt.get('done'):
                drained = False
        return drained
