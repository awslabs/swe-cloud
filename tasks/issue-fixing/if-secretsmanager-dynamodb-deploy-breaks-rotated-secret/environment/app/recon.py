"""Deployment resource refresh, plan and apply."""
import argparse
import json
import uuid

from botocore.exceptions import ClientError
from cloud import Cloud


def read_resource(aws, state):
    if not state:
        return None
    if state['secret_id']['S'] != aws.c['secret_id']:
        return None
    version = state['version_id']['S']
    try:
        return aws.sm.get_secret_value(SecretId=aws.c['secret_id'], VersionId=version)
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ResourceNotFoundException':
            return None
        raise


def plan(config):
    aws = Cloud(config)
    state = aws.state()
    value = read_resource(aws, state)
    anchor = state['version_id']['S'] if state else None
    target = value['VersionId'] if value else None
    stages = value.get('VersionStages', []) if value else []
    actions = []
    if not target:
        actions.append('create')
    elif 'AWSCURRENT' not in stages:
        actions.append('move_current')
    return {'config': config, 'anchor_before': anchor,
            'observed_holder': aws.holder(), 'target_version': target,
            'actions': actions,
            'state_serial': int(state['serial']['N']) if state else 0,
            'prior_secret_id': state['secret_id']['S'] if state else None}


def apply(change):
    aws = Cloud(change['config'])
    state = aws.state()
    target = change['target_version']
    for action in change['actions']:
        if action == 'create':
            target = aws.sm.put_secret_value(
                SecretId=aws.c['secret_id'], ClientRequestToken=str(uuid.uuid4()),
                SecretString=aws.c['secret_string'],
                VersionStages=aws.c['version_stages'])['VersionId']
        elif action == 'move_current':
            holder = aws.holder()
            args = dict(SecretId=aws.c['secret_id'], VersionStage='AWSCURRENT',
                        MoveToVersionId=target)
            if holder:
                args['RemoveFromVersionId'] = holder
            aws.sm.update_secret_version_stage(**args)
        else:
            raise ValueError('unknown action: ' + action)
    if target:
        aws.save(state, target)
    print(json.dumps({'version_id': target}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('plan')
    p.add_argument('--config', required=True)
    p.add_argument('--out', required=True)
    p = commands.add_parser('apply')
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--config')
    group.add_argument('--plan')
    args = parser.parse_args()
    if args.command == 'plan':
        with open(args.config) as stream:
            change = plan(json.load(stream))
        with open(args.out, 'w') as stream:
            json.dump(change, stream, indent=2)
    else:
        if args.config:
            with open(args.config) as stream:
                change = plan(json.load(stream))
        else:
            with open(args.plan) as stream:
                change = json.load(stream)
        apply(change)


if __name__ == '__main__':
    main()
