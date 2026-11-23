import hashlib
import json
import os

import boto3
from botocore.config import Config


def load_config():
    with open(os.environ['FANOUT_CONFIG']) as source:
        return json.load(source)


def client(config, service):
    return boto3.client(service, endpoint_url=config['endpoint_url'],
                        region_name=config.get('region', 'us-east-1'),
                        aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(retries={'max_attempts': 3},
                                      connect_timeout=3, read_timeout=8))


def upload(config, event_id, filename):
    with open(filename, 'rb') as source:
        body = source.read()
    key = config['body_prefix'] + event_id
    client(config, 's3').put_object(Bucket=config['bucket'], Key=key, Body=body)
    return {'event_id': event_id, 'body_key': key,
            'sha256': hashlib.sha256(body).hexdigest()}


def notify(config, envelope):
    client(config, 'sns').publish(TopicArn=config['topic_arn'],
                                 Message=json.dumps(envelope))


def unpack(raw):
    return json.loads(json.loads(raw)['Message'])


def hydrate(config, s3, envelope):
    body = s3.get_object(Bucket=config['bucket'], Key=envelope['body_key'])['Body'].read()
    if hashlib.sha256(body).hexdigest() != envelope['sha256']:
        raise ValueError('body digest does not match notification')
    return body


def move_to_error(config, sqs, name, queue_url, message):
    sqs.send_message(QueueUrl=config['error_queue_url'], MessageBody=message['Body'],
                     MessageAttributes={'failed_queue': {'DataType': 'String',
                                                       'StringValue': name}})
    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message['ReceiptHandle'])
