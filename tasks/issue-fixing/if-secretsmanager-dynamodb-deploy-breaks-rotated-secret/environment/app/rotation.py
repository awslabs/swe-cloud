"""Run or resume one credential rotation."""
import argparse
import hashlib
import json
import secrets
import uuid

from cloud import Cloud


def run(config):
    aws = Cloud(config)
    row = aws.ledger()
    if not row:
        raise RuntimeError('rotation journal is not initialized')
    if row['state']['S'] == 'finished':
        token = str(uuid.uuid4())
        aws.sm.put_secret_value(SecretId=config['secret_id'], ClientRequestToken=token,
                                SecretString=secrets.token_urlsafe(32),
                                VersionStages=['AWSPENDING'])
        aws.ddb.update_item(
            TableName=config['ledger_table'], Key={'secret_id': {'S': config['secret_id']}},
            UpdateExpression='SET #st = :pending, pending_token = :token, seq = seq + :one',
            ConditionExpression='#st = :finished',
            ExpressionAttributeNames={'#st': 'state'},
            ExpressionAttributeValues={':pending': {'S': 'pending'}, ':finished': {'S': 'finished'},
                                       ':token': {'S': token}, ':one': {'N': '1'}})
    else:
        token = row['pending_token']['S']
    if 'AWSCURRENT' not in aws.stages().get(token, []):
        value = aws.sm.get_secret_value(SecretId=config['secret_id'], VersionId=token)['SecretString']
        digest = hashlib.sha256(value.encode()).hexdigest()
        aws.ddb.put_item(TableName=config['db_table'], Item={
            'user': {'S': 'app'}, 'password_sha256': {'S': digest},
            'set_by_token': {'S': token}})
        actual = aws.item(config['db_table'], 'user', 'app')
        if actual['password_sha256']['S'] != digest:
            raise RuntimeError('credential test failed')
        holder = aws.holder()
        aws.sm.update_secret_version_stage(SecretId=config['secret_id'],
            VersionStage='AWSCURRENT', MoveToVersionId=token, RemoveFromVersionId=holder)
    aws.ddb.update_item(
        TableName=config['ledger_table'], Key={'secret_id': {'S': config['secret_id']}},
        UpdateExpression='SET #st = :finished, finished_version = :token',
        ConditionExpression='pending_token = :token',
        ExpressionAttributeNames={'#st': 'state'},
        ExpressionAttributeValues={':finished': {'S': 'finished'}, ':token': {'S': token}})
    print(token)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    with open(args.config) as stream:
        run(json.load(stream))
