import os
import time
import boto3
from botocore.config import Config

CONFIG = Config(region_name='us-east-1', retries={'max_attempts': 0},
                connect_timeout=5, read_timeout=90, s3={'addressing_style': 'path'})

def client(service):
    return boto3.client(service, endpoint_url=os.environ['AWS_ENDPOINT_URL'],
                        aws_access_key_id='test', aws_secret_access_key='test', config=CONFIG)

s3 = client('s3')
sqs = client('sqs')
db = boto3.resource('dynamodb', endpoint_url=os.environ['AWS_ENDPOINT_URL'],
                    aws_access_key_id='test', aws_secret_access_key='test', config=CONFIG)

def table():
    return db.Table(os.environ['ATTACHMENT_TABLE'])

def bucket():
    return os.environ['DOWNLOAD_BUCKET']

def quarantine():
    return os.environ['QUARANTINE_BUCKET']

def identity(key):
    return {'path': bucket() + '/' + key}

def record(key):
    return table().get_item(Key=identity(key), ConsistentRead=True).get('Item')

def allocate(key):
    table().put_item(Item={**identity(key), 'key': key, 'state': 'pending',
                           'expiresAt': int(time.time()) + 86400},
                     ConditionExpression='attribute_not_exists(#p)',
                     ExpressionAttributeNames={'#p': 'path'})

def expired(now):
    for state in ('pending', 'deleting'):
        cursor = {}
        while True:
            page = table().query(IndexName='PendingExpiry',
                KeyConditionExpression='#s = :s AND expiresAt <= :n',
                ExpressionAttributeNames={'#s': 'state'},
                ExpressionAttributeValues={':s': state, ':n': int(now)}, **cursor)
            yield from page.get('Items', [])
            if not page.get('LastEvaluatedKey'):
                break
            cursor = {'ExclusiveStartKey': page['LastEvaluatedKey']}
