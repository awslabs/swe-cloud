"""Small transport for the deployment's OpenSearch HTTP endpoint."""
import json
import os
import urllib.request
import urllib.parse


class OpenSearch:
    def __init__(self):
        self.endpoint = os.environ['OS_ENDPOINT'].rstrip('/')
        self.source = os.environ.get('SOURCE_INDEX', 'metrics-src')
        self.target = os.environ.get('TARGET_INDEX', 'metrics-hourly')
        self.meta = os.environ.get('META_INDEX', 'transform-meta')
        self.transform = os.environ.get('TRANSFORM_ID', 'hourly')

    def request(self, method, path, body=None, **params):
        data = body if isinstance(body, bytes) else (
            None if body is None else json.dumps(body).encode())
        url = self.endpoint + '/' + urllib.parse.quote(path.lstrip('/'), safe='/:-_')
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.load(response)
        if result.get('_shards', {}).get('failed', 0) or result.get('timed_out'):
            raise RuntimeError('Incomplete OpenSearch response')
        return result

    def write_buckets(self, documents):
        if not documents:
            return
        lines = []
        for identifier, document in documents:
            lines.extend((json.dumps({'index': {'_index': self.target, '_id': identifier}}),
                          json.dumps(document)))
        return self.request('POST', '_bulk', ('\n'.join(lines) + '\n').encode())

    def checkpoints(self):
        result = self.request('GET', self.source + '/_stats', level='shards')
        shards = result['indices'][self.source]['shards']
        return {str(s): next(x['seq_no']['global_checkpoint'] for x in copies
                            if x['routing']['primary']) for s, copies in shards.items()}
