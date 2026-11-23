"""Independent repository client, controlled over stdin by the verifier."""
import json
import os
import sys
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

endpoint, bucket, label, count_s, ledger_path = sys.argv[1:]
count = int(count_s)
ledger = {}


def client():
    return boto3.client('s3', endpoint_url=endpoint, region_name='us-east-1',
                        aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(s3={'addressing_style': 'path'},
                                      retries={'max_attempts': 2}))


s3 = client()
prefix = f'backup/{label}/'
manifest = {'label': label, 'type': 'full', 'prior': None, 'files': count}


def encode(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def put(suffix, body):
    key = prefix + suffix
    result = s3.put_object(Bucket=bucket, Key=key, Body=body)
    ledger[key] = result['ETag']
    with open(ledger_path, 'w') as stream:
        json.dump(ledger, stream)


def data(start, stop):
    for i in range(start, stop):
        # Content is private to this client; maintenance cannot regenerate it.
        put(f'pg_data/{i:04d}', os.urandom(4096))
    put('backup.manifest.copy', encode({**manifest, 'copied': stop}))


phase = 0
for line in sys.stdin:
    target = int(line.strip())
    if phase < 1 <= target:
        put('backup.manifest.copy', encode({**manifest, 'copied': 0}))
    if phase < 2 <= target:
        data(0, count // 2)
    if phase < 3 <= target:
        data(count // 2, count)
        put('backup.manifest.copy', encode(manifest))
        put('backup.manifest', encode(manifest))
    if phase < 4 <= target:
        for _ in range(32):
            result = s3.get_object(Bucket=bucket, Key='backup/backup.info')
            info = json.loads(result['Body'].read())
            result['Body'].close()
            info['current'][label] = {'type': 'full', 'prior': None, 'files': count}
            try:
                s3.put_object(Bucket=bucket, Key='backup/backup.info',
                              Body=encode(info), IfMatch=result['ETag'])
                break
            except ClientError as exc:
                if exc.response['ResponseMetadata']['HTTPStatusCode'] != 412:
                    raise
                s3.close()
                s3 = client()
        else:
            raise RuntimeError('primary catalog registration starved')
    phase = target
    print(json.dumps({'at': phase}), flush=True)
