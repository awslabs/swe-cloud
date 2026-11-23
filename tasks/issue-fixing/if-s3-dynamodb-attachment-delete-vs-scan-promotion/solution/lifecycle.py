import time
import uuid
from botocore.exceptions import ClientError
import storage as aws


def code(error, name):
    return isinstance(error, ClientError) and error.response['Error']['Code'] == name


def abort(key, upload_id):
    if upload_id:
        try:
            aws.s3.abort_multipart_upload(Bucket=aws.bucket(), Key=key, UploadId=upload_id)
        except ClientError as error:
            if not code(error, 'NoSuchUpload'):
                raise


def remove(key, pending_only=False, expired_before=None):
    names = {'#s': 'state'}
    values = {':d': 'deleting', ':n': int(time.time())}
    condition = 'attribute_exists(#p)'
    if pending_only:
        condition = '(#s = :p OR #s = :d)'
        values[':p'] = 'pending'
        if expired_before is not None:
            condition += ' AND expiresAt <= :e'
            values[':e'] = int(expired_before)
    else:
        names['#p'] = 'path'
    try:
        item = aws.table().update_item(Key=aws.identity(key),
            UpdateExpression='SET #s = :d, expiresAt = :n',
            ConditionExpression=condition, ExpressionAttributeNames=names,
            ExpressionAttributeValues=values, ReturnValues='ALL_NEW')['Attributes']
    except ClientError as error:
        if not code(error, 'ConditionalCheckFailedException'):
            raise
        if pending_only:
            if expired_before is None:
                aws.s3.delete_object(Bucket=aws.quarantine(), Key=key)
            return
        item = None
    abort(key, item.get('uploadId') if item else None)
    aws.s3.delete_object(Bucket=aws.bucket(), Key=key)
    aws.s3.delete_object(Bucket=aws.quarantine(), Key=key)
    if item:
        aws.table().delete_item(Key=aws.identity(key))


def copied(key, item):
    if not item.get('token'):
        return False
    try:
        head = aws.s3.head_object(Bucket=aws.bucket(), Key=key)
        return head.get('Metadata', {}).get('attachment-transfer') == item['token']
    except ClientError as error:
        if error.response['Error']['Code'] not in ('404', 'NotFound', 'NoSuchKey'):
            raise
        return False


def register(key, item):
    source = aws.s3.head_object(Bucket=aws.quarantine(), Key=key)
    token = uuid.uuid4().hex
    upload_id = aws.s3.create_multipart_upload(Bucket=aws.bucket(), Key=key,
        ContentType=source.get('ContentType', 'application/octet-stream'),
        Metadata={**source.get('Metadata', {}), 'attachment-transfer': token})['UploadId']
    transfer = {**item, 'uploadId': upload_id, 'token': token, 'sourceETag': source['ETag']}
    try:
        aws.table().update_item(Key=aws.identity(key),
            UpdateExpression='SET uploadId = :u, #t = :t, sourceETag = :e',
            ConditionExpression='#s = :p AND attribute_not_exists(uploadId)',
            ExpressionAttributeNames={'#s': 'state', '#t': 'token'},
            ExpressionAttributeValues={':p': 'pending', ':u': upload_id,
                                       ':t': token, ':e': source['ETag']})
        return transfer
    except Exception as error:
        current = aws.record(key)
        if current and current.get('uploadId') == upload_id:
            return current
        if not code(error, 'ConditionalCheckFailedException'):
            raise
        abort(key, upload_id)
        return current


def finish(key, item):
    if not copied(key, item):
        try:
            source = aws.s3.get_object(Bucket=aws.quarantine(), Key=key)
            if source['ETag'] != item['sourceETag']:
                raise RuntimeError('Staged source changed')
            body = source['Body'].read()
            part = aws.s3.upload_part(Bucket=aws.bucket(), Key=key,
                UploadId=item['uploadId'], PartNumber=1, Body=body)
            aws.s3.complete_multipart_upload(Bucket=aws.bucket(), Key=key,
                UploadId=item['uploadId'], MultipartUpload={
                    'Parts': [{'PartNumber': 1, 'ETag': part['ETag']}]})
        except Exception:
            if not copied(key, item):
                raise
    aws.table().update_item(Key=aws.identity(key),
        UpdateExpression='SET #s = :r REMOVE expiresAt',
        ConditionExpression='#s = :p AND uploadId = :u',
        ExpressionAttributeNames={'#s': 'state'},
        ExpressionAttributeValues={':r': 'ready', ':p': 'pending', ':u': item['uploadId']})


def promote(key):
    item = aws.record(key)
    try:
        if item and item['state'] == 'pending' and not item.get('uploadId'):
            item = register(key, item)
        if not item or item['state'] == 'deleting':
            remove(key)
            return
        if item['state'] == 'pending':
            finish(key, item)
        aws.s3.delete_object(Bucket=aws.quarantine(), Key=key)
    except Exception:
        current = aws.record(key)
        if not current or current['state'] == 'deleting':
            remove(key)
            return
        if current['state'] == 'ready' and item and current.get('uploadId') == item.get('uploadId'):
            aws.s3.delete_object(Bucket=aws.quarantine(), Key=key)
            return
        raise


def reject(key):
    remove(key, pending_only=True)


def cleanup(now):
    for item in aws.expired(now):
        key = item['key']
        if item['state'] == 'pending' and copied(key, item):
            promote(key)
        else:
            remove(key, pending_only=True, expired_before=now)
