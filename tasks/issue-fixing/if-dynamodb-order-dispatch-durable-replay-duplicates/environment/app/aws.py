import hashlib
import json
import os
import boto3
from botocore.config import Config


def client():
    return boto3.client('dynamodb', endpoint_url=os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566'),
                        region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(connect_timeout=3, read_timeout=5, retries={'max_attempts': 0}))


def payload_key(event):
    return hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
