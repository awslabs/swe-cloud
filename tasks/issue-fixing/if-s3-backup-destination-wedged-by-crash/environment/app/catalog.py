import argparse
import hashlib
from pathlib import Path
import sys
import boto3
sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy
from storage import Store, location

p = argparse.ArgumentParser(description='Provision a new backup destination')
p.add_argument('dest')
p.add_argument('--endpoint-url', required=True)
a = p.parse_args()
bucket, prefix = location(a.dest)
s = Store(a.endpoint_url, bucket)
deploy(boto3.client('cloudformation', endpoint_url=a.endpoint_url, region_name='us-east-1',
                    aws_access_key_id='test', aws_secret_access_key='test'),
       'backup-catalog-' + hashlib.sha256(bucket.encode()).hexdigest()[:24],
       Path(__file__).parent / 'infra/seed.template.json', BucketName=bucket)
try:
    s.read(prefix + '.lock')
except s.client.exceptions.NoSuchKey:
    s.put_record(prefix + '.lock', dict(state='free', owner=None, epoch=0,
                                      expires_at_ms=None, manifest_key=None))
else:
    raise SystemExit('destination already provisioned')
