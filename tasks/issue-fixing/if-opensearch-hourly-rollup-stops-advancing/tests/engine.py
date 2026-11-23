"""Verifier-owned engine setup and observations. Never imports /app."""
import json
import os
import time
import urllib.request
import urllib.parse
import uuid
import boto3
from botocore.config import Config


class Engine:
    def __init__(self, url):
        self.url = url.rstrip('/')
        self.observed_shards = {}

    def call(self, method, path, body=None, **params):
        url = self.url + '/' + urllib.parse.quote(path.lstrip('/'), safe='/:_-')
        if params:
            url += '?' + urllib.parse.urlencode(params)
        raw = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, raw, method=method,
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.load(response)
        parts = path.strip('/').split('/')
        if len(parts) == 2 and parts[1] in ('_search', '_count'):
            self.observed_shards[parts[0]] = (self.observed_shards.get(parts[0], 0)
                                             + result.get('_shards', {}).get('total', 0))
        return result

    def bulk(self, actions):
        raw = ('\n'.join(json.dumps(a) for a in actions) + '\n').encode()
        response = self.call('POST', '_bulk', raw)
        assert not response.get('errors'), response

    def stats(self, index):
        return self.call('GET', index + '/_stats', level='shards')['indices'][index]

    def checkpoint(self, index):
        stats = self.stats(index)
        return {s: next(c['seq_no']['global_checkpoint'] for c in copies
                        if c['routing']['primary']) for s, copies in stats['shards'].items()}

    def settle(self, index):
        self.call('POST', index + '/_refresh')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            copies = [c for v in self.stats(index)['shards'].values() for c in v if c['routing']['primary']]
            if all(c['seq_no']['global_checkpoint'] == c['seq_no']['max_seq_no'] for c in copies):
                return self.checkpoint(index)
            time.sleep(.1)
        raise RuntimeError('environment: global checkpoint did not settle')

    def search_total(self, index):
        return self.stats(index)['primaries']['search']['query_total']


def provision():
    try:
        aws = boto3.client('opensearch', endpoint_url=os.environ['AWS_ENDPOINT_URL'],
                            config=Config(read_timeout=600, retries={'max_attempts': 1}))
        name = 'rollup-' + uuid.uuid4().hex[:12]
        aws.create_domain(DomainName=name, EngineVersion='OpenSearch_2.11',
                          ClusterConfig={'InstanceType': 't3.small.search', 'InstanceCount': 1},
                          EBSOptions={'EBSEnabled': True, 'VolumeType': 'gp2', 'VolumeSize': 10})
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = aws.describe_domain(DomainName=name)['DomainStatus']
            if status.get('Endpoint') and not status.get('Processing', False):
                break
            time.sleep(1)
        else:
            raise RuntimeError('domain did not become ready')
        url = status['Endpoint']
        if not url.startswith('http'):
            url = 'http://' + url
        engine = Engine(url)
        for _ in range(240):
            try:
                info = engine.call('GET', '')
                assert info['version']['number'].startswith('2.11.'), info
                return engine, name, aws
            except OSError:
                time.sleep(1)
        raise RuntimeError('endpoint unreachable')
    except Exception as exc:
        raise RuntimeError('environment: OpenSearch preflight: ' + repr(exc)) from exc


SOURCES = [
    {'bucket_start_ms': {'date_histogram': {'field': 'ts', 'fixed_interval': '60m'}}},
    {'device': {'terms': {'field': 'device'}}}]


def oracle(engine, index, upper, transform):
    after = None
    result = {}
    while True:
        composite = {'size': 1000, 'sources': SOURCES}
        if after is not None:
            composite['after'] = after
        response = engine.call('POST', index + '/_search', {
            'size': 0, 'track_total_hits': True,
            'query': {'bool': {'filter': [{'range': {'ts': {'gt': upper - 48*3600000, 'lte': upper}}}],
                                'must_not': [{'term': {'voided': True}}]}},
            'aggs': {'rows': {'composite': composite, 'aggs': {
                's': {'sum': {'field': 'm1'}}, 'n': {'value_count': {'field': 'm1'}},
                'a': {'avg': {'field': 'm1'}}}}}})
        assert not response.get('timed_out') and response['_shards']['failed'] == 0
        rows = response['aggregations']['rows']
        for b in rows['buckets']:
            key = b['key']
            identifier = f"{transform}#{key['bucket_start_ms']}:{key['device']}"
            result[identifier] = {**key, 'doc_count': b['doc_count'], 'm1_sum': b['s']['value'],
                                  'm1_count': b['n']['value'], 'm1_avg': b['a']['value']}
        if len(rows['buckets']) < 1000 or not rows.get('after_key'):
            return result
        after = rows['after_key']


def target_docs(engine, index):
    result = engine.call('POST', index + '/_search', {'size': 10000, 'query': {'match_all': {}},
                                                    'track_total_hits': True})
    assert result['hits']['total']['relation'] == 'eq'
    assert result['hits']['total']['value'] <= 10000
    return {h['_id']: h['_source'] for h in result['hits']['hits']}
