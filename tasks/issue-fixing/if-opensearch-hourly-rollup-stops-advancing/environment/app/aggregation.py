"""Composite bucket discovery and whole-bucket materialization."""


def selected(upper):
    return {'bool': {'filter': [{'range': {'ts': {'gt': upper - 48 * 3600000,
                                                'lte': upper}}}],
                     'must_not': [{'term': {'voided': True}}]}}


def composite(client, query, config, metrics=False, preference=None):
    size = config['page_size']
    after = None
    while True:
        grouping = {'size': size, 'sources': [
            {'bucket_start_ms': {'date_histogram': {'field': 'ts',
                'fixed_interval': str(config['bucket_minutes']) + 'm'}}},
            {'device': {'terms': {'field': 'device'}}}]}
        if after is not None:
            grouping['after'] = after
        agg = {'composite': grouping}
        if metrics:
            agg['aggs'] = {name: {kind: {'field': 'm1'}} for name, kind in (
                ('m1_sum', 'sum'), ('m1_count', 'value_count'), ('m1_avg', 'avg'))}
        body = {'size': 0, 'query': query, 'aggs': {'rows': agg}}
        params = {} if preference is None else {'preference': preference}
        response = client.request('POST', client.source + '/_search', body, **params)
        page = response['aggregations']['rows']
        yield from page['buckets']
        if len(page['buckets']) < size or not page.get('after_key'):
            return
        after = page['after_key']


def keys(client, query, config, preference=None):
    return {(b['key']['bucket_start_ms'], b['key']['device'])
            for b in composite(client, query, config, preference=preference)}


def rows(client, dirty, upper, config):
    interval = config['bucket_minutes'] * 60000
    ordered = sorted(dirty)
    for offset in range(0, len(ordered), 128):
        clauses = [{'bool': {'filter': [
            {'range': {'ts': {'gte': start, 'lt': start + interval}}},
            {'term': {'device': device}}]}} for start, device in ordered[offset:offset + 128]]
        query = {'bool': {'filter': [selected(upper)], 'should': clauses,
                          'minimum_should_match': 1}}
        for bucket in composite(client, query, config, metrics=True):
            key = bucket['key']
            doc = {**key, 'doc_count': bucket['doc_count']}
            doc.update({metric: bucket[metric]['value']
                        for metric in ('m1_sum', 'm1_count', 'm1_avg')})
            identifier = f"{client.transform}#{key['bucket_start_ms']}:{key['device']}"
            yield identifier, doc


def materialize(client, dirty, upper, config):
    client.write_buckets(list(rows(client, dirty, upper, config)))
