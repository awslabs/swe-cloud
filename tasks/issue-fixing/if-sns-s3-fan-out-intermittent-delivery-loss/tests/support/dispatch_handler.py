"""Application callback owned by the verifier, never installed into /app."""
import hashlib
import json
import os
import time
import uuid

import boto3


def handle(subscriber, event_id, body):
    with open(os.environ['FANOUT_CONFIG']) as source:
        config = json.load(source)
    ddb = boto3.client('dynamodb', endpoint_url=config['endpoint_url'],
                        region_name='us-east-1', aws_access_key_id='test',
                        aws_secret_access_key='test')
    key = subscriber + '#' + event_id
    directive = ddb.get_item(TableName=config['control_table'],
                             Key={'pk': {'S': key}}, ConsistentRead=True).get('Item', {})
    if directive.get('blocked', {}).get('BOOL'):
        raise RuntimeError('projection dependency temporarily unavailable')
    # Verify source existence at actual application dispatch, independently of transport code.
    s3 = boto3.client('s3', endpoint_url=config['endpoint_url'], region_name='us-east-1',
                       aws_access_key_id='test', aws_secret_access_key='test')
    source = s3.get_object(Bucket=config['bucket'], Key=config['body_prefix'] + event_id)['Body'].read()
    if source != body:
        raise RuntimeError('application received bytes different from body store')
    ddb.put_item(TableName=config['receipt_table'], Item={
        'pk': {'S': key + '#' + uuid.uuid4().hex},
        'subscriber': {'S': subscriber}, 'event_id': {'S': event_id},
        'digest': {'S': hashlib.sha256(body).hexdigest()},
        'observed_at': {'N': str(time.time())}})
