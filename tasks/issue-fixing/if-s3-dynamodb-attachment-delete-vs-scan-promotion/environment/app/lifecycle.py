import storage as aws


def promote(key):
    aws.s3.copy_object(Bucket=aws.bucket(), Key=key,
                       CopySource={'Bucket': aws.quarantine(), 'Key': key})
    aws.table().update_item(Key=aws.identity(key),
        UpdateExpression='SET #s = :r REMOVE expiresAt',
        ExpressionAttributeNames={'#s': 'state'},
        ExpressionAttributeValues={':r': 'ready'})
    aws.s3.delete_object(Bucket=aws.quarantine(), Key=key)


def remove(key):
    aws.s3.delete_object(Bucket=aws.bucket(), Key=key)
    aws.table().delete_item(Key=aws.identity(key))


def reject(key):
    aws.s3.delete_object(Bucket=aws.quarantine(), Key=key)


def cleanup(now):
    for item in aws.expired(now):
        remove(item['key'])
