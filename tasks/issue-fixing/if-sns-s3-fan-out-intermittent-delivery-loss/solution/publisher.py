"""Durable publication intent; SNS acceptance can be retried without another PUT."""
import hashlib
import time

from botocore.exceptions import ClientError
from seam import seam
from transport import client, notify
import pump

LEASE = 3


def conflict(exc):
    return exc.response['Error']['Code'] == 'ConditionalCheckFailedException'


def envelope(row):
    return {key: row[key]['S'] for key in ('event_id', 'body_key', 'sha256')}


def reserve(config, db, event):
    item = {key: {'S': value} for key, value in event.items()}
    item.update(pending={'SS': list(config['subscribers'])},
                stage={'S': 'uploading'}, retry_at={'N': str(time.time() + LEASE)})
    try:
        db.put_item(TableName=pump.table(config), Item=item,
                    ConditionExpression='attribute_not_exists(event_id)')
        return item
    except ClientError as exc:
        if not conflict(exc):
            raise
    row = pump.read(config, db, event['event_id'])
    if envelope(row) != event:
        raise ValueError('event ID is already bound to different bytes')
    return row


def activate(config, db, event_id):
    try:
        db.update_item(TableName=pump.table(config),
                        Key={'event_id': {'S': event_id}},
                        UpdateExpression='SET stage = :ready',
                        ConditionExpression='stage = :uploading',
                        ExpressionAttributeValues={':ready': {'S': 'ready'},
                                                   ':uploading': {'S': 'uploading'}})
    except ClientError as exc:
        if not conflict(exc):
            raise
    return pump.read(config, db, event_id)


def accepted(config, db, event_id):
    db.update_item(TableName=pump.table(config), Key={'event_id': {'S': event_id}},
                    UpdateExpression='SET published = :yes REMOVE retry_at',
                    ExpressionAttributeValues={':yes': {'BOOL': True}})


def send(config, db, row, instrument=False):
    if row.get('published', {}).get('BOOL'):
        return
    event = envelope(row)
    notify(config, event)
    if instrument:
        seam('after_notify', subscriber='publisher', event_id=event['event_id'])
    accepted(config, db, event['event_id'])


def uploaded(config, s3, event):
    try:
        # This read is only for an unfinished upload. Ready/completed records
        # must never be used as permission to PUT or to require an extant body.
        body = s3.get_object(Bucket=config['bucket'], Key=event['body_key'])['Body'].read()
    except ClientError as exc:
        if exc.response['Error']['Code'] in ('NoSuchKey', '404', 'NotFound'):
            return False
        raise
    if hashlib.sha256(body).hexdigest() != event['sha256']:
        raise ValueError('uploaded object conflicts with publication intent')
    return True


def publish(config, event_id, filename):
    with open(filename, 'rb') as source:
        body = source.read()
    event = {'event_id': event_id, 'body_key': config['body_prefix'] + event_id,
             'sha256': hashlib.sha256(body).hexdigest()}
    db, s3 = client(config, 'dynamodb'), client(config, 's3')
    row = reserve(config, db, event)
    if row['stage']['S'] == 'uploading':
        if not uploaded(config, s3, event):
            # Absence may mean a recoverer already activated and completed
            # this publication since reserve(). Re-read before any PUT.
            row = pump.read(config, db, event_id)
            if row['stage']['S'] == 'uploading':
                s3.put_object(Bucket=config['bucket'], Key=event['body_key'], Body=body)
        # Intent precedes PUT. Recovery can discover that PUT even if this
        # caller never executes the next instruction or retains its body file.
        seam('after_upload', subscriber='publisher', event_id=event_id)
        row = activate(config, db, event_id)
    send(config, db, row, instrument=True)


def recover(config, db, s3, snapshot):
    if snapshot.get('published', {}).get('BOOL'):
        return
    now = time.time()
    try:
        row = db.update_item(
            TableName=pump.table(config), Key={'event_id': snapshot['event_id']},
            UpdateExpression='SET retry_at = :later',
            ConditionExpression='attribute_not_exists(published) AND retry_at <= :now',
            ExpressionAttributeValues={':now': {'N': str(now)},
                                       ':later': {'N': str(now + LEASE)}},
            ReturnValues='ALL_NEW')['Attributes']
    except ClientError as exc:
        if conflict(exc):
            return
        raise
    if row['stage']['S'] == 'uploading':
        if not uploaded(config, s3, envelope(row)):
            return  # Interrupted before upload: the upstream producer can retry.
        row = activate(config, db, row['event_id']['S'])
    # Pending membership may already be empty: an earlier SNS call can have
    # succeeded while its durable acceptance is still unknown. Duplicates then
    # use completion evidence, never a recreated S3 object.
    send(config, db, row)
