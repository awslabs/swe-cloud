"""AWS transport and deployment state persistence."""
import os

import boto3
from botocore.config import Config


def client(service):
    return boto3.client(
        service, endpoint_url=os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566'),
        region_name=os.environ.get('AWS_REGION', 'us-east-1'),
        aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID', 'test'),
        aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY', 'test'),
        config=Config(connect_timeout=3, read_timeout=5, retries={'max_attempts': 2}))


class Cloud:
    def __init__(self, config):
        self.c = config
        self.sm = client('secretsmanager')
        self.ddb = client('dynamodb')

    def item(self, table, key, value):
        return self.ddb.get_item(TableName=table, Key={key: {'S': value}},
                                 ConsistentRead=True).get('Item')

    def state(self):
        return self.item(self.c['state_table'], 'resource_id', self.c['resource_id'])

    def ledger(self):
        return self.item(self.c['ledger_table'], 'secret_id', self.c['secret_id'])

    def stages(self):
        return self.sm.describe_secret(SecretId=self.c['secret_id']).get('VersionIdsToStages', {})

    def holder(self):
        args = dict(SecretId=self.c['secret_id'], IncludeDeprecated=True)
        while True:
            page = self.sm.list_secret_version_ids(**args)
            for version in page['Versions']:
                if 'AWSCURRENT' in version.get('VersionStages', []):
                    return version['VersionId']
            token = page.get('NextToken')
            if not token:
                return None
            args['NextToken'] = token

    def save(self, before, version):
        serial = int(before['serial']['N']) if before else 0
        item = {'resource_id': {'S': self.c['resource_id']},
                'secret_id': {'S': self.c['secret_id']},
                'version_id': {'S': version}, 'serial': {'N': str(serial + 1)}}
        kwargs = {'ConditionExpression': 'attribute_not_exists(resource_id)'}
        if before:
            kwargs = {'ConditionExpression': '#s = :old',
                      'ExpressionAttributeNames': {'#s': 'serial'},
                      'ExpressionAttributeValues': {':old': {'N': str(serial)}}}
        self.ddb.put_item(TableName=self.c['state_table'], Item=item, **kwargs)
