import json
from urllib.parse import urlparse

import boto3
from botocore.config import Config


def location(dest):
    parsed = urlparse(dest)
    if parsed.scheme != 's3' or not parsed.netloc or not parsed.path.strip('/'):
        raise ValueError('expected s3://bucket/prefix/')
    return parsed.netloc, parsed.path.strip('/') + '/'


class Store:
    def __init__(self, endpoint, bucket):
        self.endpoint, self.bucket = endpoint, bucket
        self.reset()

    def reset(self):
        if hasattr(self, 'client'):
            self.client.close()
        self.client = boto3.client(
            's3', endpoint_url=self.endpoint, region_name='us-east-1',
            aws_access_key_id='test', aws_secret_access_key='test',
            config=Config(s3={'addressing_style': 'path'},
                          retries={'max_attempts': 0}, connect_timeout=3, read_timeout=20))

    def read(self, key):
        reply = self.client.get_object(Bucket=self.bucket, Key=key)
        return reply['Body'].read(), reply['ETag']

    def record(self, key):
        raw, etag = self.read(key)
        return json.loads(raw), etag

    def put(self, key, data, **headers):
        return self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **headers)['ETag']

    def put_record(self, key, body, **headers):
        return self.put(key, json.dumps(body, sort_keys=True).encode(), **headers)
