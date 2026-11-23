import json
import os
import boto3
from botocore.config import Config


def client(service):
    return boto3.client(service, endpoint_url=os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566'),
                        region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(connect_timeout=2, read_timeout=5,
                                      retries={'max_attempts': 0}, s3={'addressing_style': 'path'}))


def load(s3, bucket, key):
    response = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(response['Body'].read()), response['ETag']


def encode(doc):
    return json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()
