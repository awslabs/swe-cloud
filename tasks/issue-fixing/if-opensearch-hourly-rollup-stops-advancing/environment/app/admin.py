"""Provision an empty development deployment through AWS APIs."""
import argparse
import json
import os
import time
import urllib.request
import boto3
from botocore.config import Config


def provision(name, now_ms):
    aws = boto3.client('opensearch', endpoint_url=os.environ['AWS_ENDPOINT_URL'],
                       config=Config(read_timeout=600, retries={'max_attempts': 1}))
    aws.create_domain(DomainName=name, EngineVersion='OpenSearch_2.11',
                      ClusterConfig={'InstanceType': 't3.small.search', 'InstanceCount': 1},
                      EBSOptions={'EBSEnabled': True, 'VolumeType': 'gp2', 'VolumeSize': 10})
    for _ in range(120):
        status = aws.describe_domain(DomainName=name)['DomainStatus']
        if status.get('Endpoint') and not status.get('Processing', False):
            break
        time.sleep(1)
    else:
        raise RuntimeError('Domain did not become available')
    endpoint = status['Endpoint']
    if not endpoint.startswith('http'):
        endpoint = 'http://' + endpoint
    for _ in range(60):
        try:
            urllib.request.urlopen(endpoint, timeout=2).close()
            break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError('Domain endpoint unreachable')
    properties = {'ts': {'type': 'date', 'format': 'epoch_millis'},
                  'device': {'type': 'keyword'}, 'm1': {'type': 'long'},
                  'voided': {'type': 'boolean'}}
    for index in ('metrics-src', 'metrics-hourly', 'transform-meta'):
        body = {'settings': {'number_of_shards': 3 if index == 'metrics-src' else 1,
                             'number_of_replicas': 0}}
        if index == 'metrics-src':
            body['mappings'] = {'properties': properties}
        request = urllib.request.Request(endpoint + '/' + index, json.dumps(body).encode(),
                                         method='PUT', headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(request, timeout=30).close()
    progress = {'shard_checkpoints': {'0': -1, '1': -1, '2': -1},
                'selection_upper_bound_ms': now_ms - 3600000, 'status': 'committed'}
    request = urllib.request.Request(endpoint + '/transform-meta/_doc/hourly',
                                     json.dumps(progress).encode(), method='PUT',
                                     headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(request, timeout=30).close()
    print('OS_ENDPOINT=' + endpoint)
    print('Empty backfill committed at supplied clock; deployment is ready for ingestion.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('provision')
    create.add_argument('--domain', required=True)
    create.add_argument('--now-ms', type=int, required=True)
    args = parser.parse_args()
    provision(args.domain, args.now_ms)
