"""Deployment resource refresh, plan and apply."""
import argparse
import json
import uuid

from botocore.exceptions import ClientError
from cloud import Cloud


def read_resource(aws, state):
    if not state:
        return None
    version = state['version_id']['S']
    try:
        return aws.sm.get_secret_value(SecretId=aws.c['secret_id'], VersionId=version)
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ResourceNotFoundException':
            return None
        raise


def bound_to(aws, state):
    return state and state['secret_id']['S'] == aws.c['secret_id']


def save_binding(aws, before, version):
    serial = int(before['serial']['N']) if before else 0
    # This watermark advances on replacement, not on ordinary rotation refresh.
    floor = int(before.get('bound_since', {'N': '0'})['N']) if before else 0
    if before and not bound_to(aws, before):
        floor = serial + 1
    item = dict(before or {})
    item.update(resource_id={'S': aws.c['resource_id']},
                secret_id={'S': aws.c['secret_id']}, version_id={'S': version},
                serial={'N': str(serial + 1)}, bound_since={'N': str(floor)})
    kwargs = {'ConditionExpression': 'attribute_not_exists(resource_id)'}
    if before:
        kwargs = {'ConditionExpression': '#s = :old',
                  'ExpressionAttributeNames': {'#s': 'serial'},
                  'ExpressionAttributeValues': {':old': {'N': str(serial)}}}
    aws.ddb.put_item(TableName=aws.c['state_table'], Item=item, **kwargs)
    return item


def decision(aws):
    state = aws.state()
    ledger = aws.ledger()
    labels = aws.stages()
    if ledger != aws.ledger():
        return decision(aws)
    holder = next((v for v, stages in labels.items() if 'AWSCURRENT' in stages), None)
    anchor_before = state['version_id']['S'] if state else None
    anchor = anchor_before if bound_to(aws, state) else None
    target = anchor
    if ledger:
        target = ledger['finished_version']['S']
        pending = ledger.get('pending_token', {}).get('S')
        if ledger['state']['S'] == 'pending' and (
                pending == holder or pending == anchor or
                'AWSPREVIOUS' in labels.get(pending, [])):
            target = pending
    if target is None:
        if ledger or bound_to(aws, state):
            raise RuntimeError('cannot bootstrap an existing installation without ownership evidence')
        actions = ['create']
    else:
        if target not in labels:
            aws.sm.get_secret_value(SecretId=aws.c['secret_id'], VersionId=target)
        actions = ['move_current'] if holder != target else []
    return state, {'config': aws.c, 'anchor_before': anchor_before,
                   'observed_holder': holder, 'target_version': target, 'actions': actions,
                   'state_serial': int(state['serial']['N']) if state else 0,
                   'prior_secret_id': state['secret_id']['S'] if state else None}


def plan(config):
    return decision(Cloud(config))[1]


def apply(change):
    aws = Cloud(change['config'])
    state = aws.state()
    floor = int(state.get('bound_since', {'N': '0'})['N']) if state else 0
    if change['state_serial'] < floor:
        print(json.dumps({'version_id': state['version_id']['S'], 'retired': True}))
        return
    for attempt in range(8):
        state, fresh = decision(aws)
        target = fresh['target_version']
        try:
            if fresh['actions'] == ['create']:
                token = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([
                    aws.c['state_table'], aws.c['resource_id'], aws.c['secret_id']])))
                target = aws.sm.put_secret_value(
                    SecretId=aws.c['secret_id'], ClientRequestToken=token,
                    SecretString=aws.c['secret_string'], VersionStages=['AWSCURRENT'])['VersionId']
                save_binding(aws, state, target)
            else:
                # A binding change is committed only after destination convergence.
                # In-place refresh can retain publication evidence before the move.
                replacing = state and not bound_to(aws, state)
                if not replacing:
                    save_binding(aws, state, target)
                    state, fresh = decision(aws)
                    target = fresh['target_version']
                if fresh['actions'] == ['move_current']:
                    args = dict(SecretId=aws.c['secret_id'], VersionStage='AWSCURRENT',
                                MoveToVersionId=target)
                    if fresh['observed_holder']:
                        args['RemoveFromVersionId'] = fresh['observed_holder']
                    aws.sm.update_secret_version_stage(**args)
                if replacing or not state or state['version_id']['S'] != target:
                    save_binding(aws, state, target)
            print(json.dumps({'version_id': target}))
            return
        except ClientError as exc:
            code = exc.response['Error']['Code']
            if code not in ('InvalidParameterException', 'InvalidRequestException',
                            'ConditionalCheckFailedException') or attempt == 7:
                raise
    raise RuntimeError('deployment did not converge')


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
