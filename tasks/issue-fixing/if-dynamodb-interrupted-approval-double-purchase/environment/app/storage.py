import os
import time

import boto3
from botocore.exceptions import ClientError


def now_ms():
    return int(time.time() * 1000)


def lost_race(exc):
    return isinstance(exc, ClientError) and exc.response['Error']['Code'] == 'ConditionalCheckFailedException'


class Store:
    def __init__(self):
        self.db = boto3.resource('dynamodb', endpoint_url=os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566'),
                                 region_name=os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
        self.table = self.db.Table(os.environ.get('EXECUTIONS_TABLE', 'executions'))

    def init(self):
        try:
            self.db.create_table(TableName=self.table.name,
                                 KeySchema=[{'AttributeName': 'execution_id', 'KeyType': 'HASH'}],
                                 AttributeDefinitions=[{'AttributeName': 'execution_id', 'AttributeType': 'S'}],
                                 BillingMode='PAY_PER_REQUEST')
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ResourceInUseException':
                raise
        self.table.wait_until_exists()

    def get(self, execution_id):
        return self.table.get_item(Key={'execution_id': execution_id}, ConsistentRead=True).get('Item')

    def scan(self):
        args = {'ConsistentRead': True}
        while True:
            page = self.table.scan(**args)
            yield from page.get('Items', [])
            if not page.get('LastEvaluatedKey'):
                break
            args['ExclusiveStartKey'] = page['LastEvaluatedKey']

    def change(self, execution_id, update, values, condition, names=None):
        args = dict(Key={'execution_id': execution_id}, UpdateExpression=update,
                    ExpressionAttributeValues=values, ConditionExpression=condition,
                    ReturnValues='ALL_NEW')
        if names:
            args['ExpressionAttributeNames'] = names
        return self.table.update_item(**args)['Attributes']

    def save(self, row):
        self.table.put_item(Item=row)
