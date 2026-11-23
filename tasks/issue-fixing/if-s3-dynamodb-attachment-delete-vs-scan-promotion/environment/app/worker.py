import argparse
import json
import os
import sys
import time
import traceback
import lifecycle
import storage as aws


def provision(prefix):
    for name in (prefix + '-quarantine', prefix + '-downloads'):
        aws.s3.create_bucket(Bucket=name)
    aws.client('dynamodb').create_table(TableName=prefix + '-attachments',
        KeySchema=[{'AttributeName': 'path', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'path', 'AttributeType': 'S'},
                              {'AttributeName': 'state', 'AttributeType': 'S'},
                              {'AttributeName': 'expiresAt', 'AttributeType': 'N'}],
        BillingMode='PAY_PER_REQUEST', GlobalSecondaryIndexes=[{
            'IndexName': 'PendingExpiry', 'KeySchema': [
                {'AttributeName': 'state', 'KeyType': 'HASH'},
                {'AttributeName': 'expiresAt', 'KeyType': 'RANGE'}],
            'Projection': {'ProjectionType': 'ALL'}}])
    result = {'QUARANTINE_BUCKET': prefix + '-quarantine',
              'DOWNLOAD_BUCKET': prefix + '-downloads',
              'ATTACHMENT_TABLE': prefix + '-attachments'}
    for role, visibility in [('SCAN', 60), ('DELETE', 5), ('EVENTS', 60)]:
        result[role + '_QUEUE_URL'] = aws.sqs.create_queue(
            QueueName=prefix + '-' + role.lower(),
            Attributes={'VisibilityTimeout': str(visibility)})['QueueUrl']
    print(json.dumps(result))


def consume(role):
    url = os.environ[role + '_QUEUE_URL']
    while True:
        messages = aws.sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1,
                                          WaitTimeSeconds=1).get('Messages', [])
        for message in messages:
            try:
                body = json.loads(message['Body'])
                key = body['key']
                if role == 'DELETE':
                    lifecycle.remove(key)
                    aws.sqs.send_message(QueueUrl=os.environ['EVENTS_QUEUE_URL'],
                        MessageBody=json.dumps({'event': 'deleted', 'key': key}))
                elif body.get('result') == 'clean':
                    lifecycle.promote(key)
                else:
                    lifecycle.reject(key)
                aws.sqs.delete_message(QueueUrl=url, ReceiptHandle=message['ReceiptHandle'])
            except Exception:
                traceback.print_exc(file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description='Accounts attachment service')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('provision').add_argument('--prefix', required=True)
    sub.add_parser('allocate').add_argument('--key', required=True)
    sub.add_parser('promote-worker')
    sub.add_parser('delete-worker')
    sub.add_parser('cleanup').add_argument('--now', type=int, default=int(time.time()))
    args = parser.parse_args()
    if args.command == 'provision':
        provision(args.prefix)
    elif args.command == 'allocate':
        aws.allocate(args.key)
    elif args.command == 'cleanup':
        lifecycle.cleanup(args.now)
    else:
        consume('SCAN' if args.command == 'promote-worker' else 'DELETE')

if __name__ == '__main__':
    main()
