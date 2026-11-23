import importlib
import signal
import time
import traceback

from botocore.exceptions import ClientError
from seam import seam
from transport import client, hydrate, move_to_error, unpack


def table(config):
    return config['state_prefix'] + 'body_refs'


def setup(config):
    ddb = client(config, 'dynamodb')
    try:
        ddb.create_table(TableName=table(config),
                         KeySchema=[{'AttributeName': 'event_id', 'KeyType': 'HASH'}],
                         AttributeDefinitions=[{'AttributeName': 'event_id', 'AttributeType': 'S'}],
                         BillingMode='PAY_PER_REQUEST')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ResourceInUseException':
            raise
    ddb.get_waiter('table_exists').wait(TableName=table(config))


def prepare(config, envelope):
    client(config, 'dynamodb').put_item(
        TableName=table(config),
        Item={'event_id': {'S': envelope['event_id']},
              'body_key': {'S': envelope['body_key']},
              'pending': {'SS': list(config['subscribers'])}},
        ConditionExpression='attribute_not_exists(event_id)')


def read(config, ddb, event_id):
    return ddb.get_item(TableName=table(config),
                        Key={'event_id': {'S': event_id}},
                        ConsistentRead=True).get('Item')


def needed(row, name):
    if row is None:
        raise RuntimeError('publication state is unavailable')
    return name in row.get('pending', {}).get('SS', [])


def release(config, ddb, event_id, name):
    try:
        return ddb.update_item(
            TableName=table(config), Key={'event_id': {'S': event_id}},
            UpdateExpression='DELETE pending :one',
            ConditionExpression='contains(pending, :name)',
            ExpressionAttributeValues={':one': {'SS': [name]}, ':name': {'S': name}},
            ReturnValues='ALL_NEW')['Attributes']
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return read(config, ddb, event_id)


def reclaim(config, ddb, s3, row):
    if row is None or row.get('pending') or row.get('reclaimed', {}).get('BOOL'):
        return
    # Pending membership only shrinks; an empty set can never become live again.
    s3.delete_object(Bucket=config['bucket'], Key=row['body_key']['S'])
    ddb.update_item(TableName=table(config),
                    Key={'event_id': row['event_id']},
                    UpdateExpression='SET reclaimed = :yes',
                    ConditionExpression='attribute_not_exists(pending)',
                    ExpressionAttributeValues={':yes': {'BOOL': True}})


def process(config, name, sqs, s3, handler, message):
    queue = config['subscribers'][name]
    envelope = unpack(message['Body'])
    event_id = envelope['event_id']
    ddb = client(config, 'dynamodb')
    row = read(config, ddb, event_id)
    if not needed(row, name):
        sqs.delete_message(QueueUrl=queue, ReceiptHandle=message['ReceiptHandle'])
        reclaim(config, ddb, s3, row)
        return
    try:
        body = hydrate(config, s3, envelope)
    except Exception:
        # A concurrent worker may have finished this logical projection.
        if needed(read(config, ddb, event_id), name):
            move_to_error(config, sqs, name, queue, message)
        else:
            sqs.delete_message(QueueUrl=queue, ReceiptHandle=message['ReceiptHandle'])
        return
    facts = {'subscriber': name, 'event_id': event_id}
    seam('after_fetch', **facts)
    try:
        handler.handle(name, event_id, body)
    except Exception:
        traceback.print_exc()
        if int(message.get('Attributes', {}).get('ApproximateReceiveCount', '1')) >= config.get('handler_attempts', 3):
            move_to_error(config, sqs, name, queue, message)
        return
    seam('after_handler', **facts)
    row = release(config, ddb, event_id, name)
    seam('after_record', **facts)
    sqs.delete_message(QueueUrl=queue, ReceiptHandle=message['ReceiptHandle'])
    seam('after_ack', **facts)
    reclaim(config, ddb, s3, row)


def maintenance(config, s3):
    ddb = client(config, 'dynamodb')
    request = {'TableName': table(config), 'ConsistentRead': True}
    while True:
        result = ddb.scan(**request)
        for row in result.get('Items', []):
            import publisher
            publisher.recover(config, ddb, s3, row)
            reclaim(config, ddb, s3, row)
        if 'LastEvaluatedKey' not in result:
            break
        request['ExclusiveStartKey'] = result['LastEvaluatedKey']


def run(config, name):
    sqs, s3 = client(config, 'sqs'), client(config, 's3')
    handler = importlib.import_module(config.get('handler_module', 'business'))
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print('READY ' + name, flush=True)
    next_maintenance = 0
    while running:
        try:
            if time.monotonic() >= next_maintenance:
                maintenance(config, s3)
                next_maintenance = time.monotonic() + 1
            result = sqs.receive_message(QueueUrl=config['subscribers'][name],
                                         MaxNumberOfMessages=1, WaitTimeSeconds=1,
                                         AttributeNames=['All'])
            for message in result.get('Messages', []):
                process(config, name, sqs, s3, handler, message)
        except Exception:
            traceback.print_exc()
            time.sleep(0.2)
