import importlib
import signal
import time
import traceback

from seam import seam
from transport import client, hydrate, move_to_error, unpack


def setup(config):
    pass


def process(config, name, sqs, s3, handler, message):
    queue = config['subscribers'][name]
    try:
        envelope = unpack(message['Body'])
        body = hydrate(config, s3, envelope)
    except Exception:
        traceback.print_exc()
        move_to_error(config, sqs, name, queue, message)
        return
    facts = {'subscriber': name, 'event_id': envelope['event_id']}
    seam('after_fetch', **facts)
    try:
        handler.handle(name, envelope['event_id'], body)
    except Exception:
        traceback.print_exc()
        if int(message.get('Attributes', {}).get('ApproximateReceiveCount', '1')) >= config.get('handler_attempts', 3):
            move_to_error(config, sqs, name, queue, message)
        return
    seam('after_handler', **facts)
    seam('after_record', **facts)
    sqs.delete_message(QueueUrl=queue, ReceiptHandle=message['ReceiptHandle'])
    seam('after_ack', **facts)
    try:
        s3.delete_object(Bucket=config['bucket'], Key=envelope['body_key'])
    except Exception:
        traceback.print_exc()


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
    while running:
        try:
            result = sqs.receive_message(QueueUrl=config['subscribers'][name],
                                         MaxNumberOfMessages=1, WaitTimeSeconds=1,
                                         AttributeNames=['All'])
            for message in result.get('Messages', []):
                process(config, name, sqs, s3, handler, message)
        except Exception:
            traceback.print_exc()
            time.sleep(0.2)
