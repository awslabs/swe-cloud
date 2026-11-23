"""Provision an ordinary local installation with unique service names."""
import argparse
import hashlib
import json
import subprocess
import uuid
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy
from cloud import Cloud, client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    suffix = uuid.uuid4().hex[:12]
    config = dict(resource_id='database_password_' + suffix, secret_id='site/db/' + suffix,
                  secret_string='initial-' + uuid.uuid4().hex, version_stages=['AWSCURRENT'],
                  state_table='deployment_' + suffix, ledger_table='rotation_' + suffix,
                  db_table='database_' + suffix)
    ddb = client('dynamodb')
    deploy(client('cloudformation'), 'credential-app-seed-' + suffix,
           Path(__file__).parent / 'infra/seed.template.json',
           StateTable=config['state_table'], LedgerTable=config['ledger_table'],
           DbTable=config['db_table'])
    for table in ('state_table', 'ledger_table', 'db_table'):
        ddb.get_waiter('table_exists').wait(TableName=config[table])
    client('secretsmanager').create_secret(Name=config['secret_id'])
    Path(args.out).write_text(json.dumps(config, indent=2))
    subprocess.run([str(Path(__file__).with_name('recon')), 'apply', '--config', args.out], check=True)
    aws = Cloud(config)
    version = aws.holder()
    ddb.put_item(TableName=config['ledger_table'], Item={
        'secret_id': {'S': config['secret_id']}, 'state': {'S': 'finished'},
        'finished_version': {'S': version}, 'pending_token': {'S': version}, 'seq': {'N': '0'}})
    ddb.put_item(TableName=config['db_table'], Item={
        'user': {'S': 'app'}, 'set_by_token': {'S': version},
        'password_sha256': {'S': hashlib.sha256(config['secret_string'].encode()).hexdigest()}})
    print(args.out)


if __name__ == '__main__':
    main()
