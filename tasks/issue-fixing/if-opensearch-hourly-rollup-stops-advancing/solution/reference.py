"""Continuous materialization with durable, finite delivery work."""
import argparse
import json
import urllib.error
from pathlib import Path
from os_http import OpenSearch
from aggregation import selected, keys, rows


class RetryDispatch(Exception):
    pass


class OwnedClient(OpenSearch):
    """Keep the metadata revision and the target ordering in the same handoff."""
    def claim(self, path, response):
        self.revision = (response['_seq_no'], response['_primary_term'])
        record = response['_source']
        self.generation = record.get('generation', 1) + 1
        record['generation'] = self.generation
        self.save(path, record)
        return record

    def save(self, path, record):
        response = self.request('PUT', path, record, if_seq_no=self.revision[0],
                                if_primary_term=self.revision[1])
        self.revision = (response['_seq_no'], response['_primary_term'])

    def write_buckets(self, documents):
        lines = []
        for identifier, document in documents:
            lines.extend((json.dumps({'index': {'_index': self.target, '_id': identifier,
                'version': self.generation, 'version_type': 'external_gte'}}), json.dumps(document)))
        return self.request('POST', '_bulk', ('\n'.join(lines) + '\n').encode())


def save(client, path, record):
    client.save(path, record)


def target_keys(client, query):
    """Read materialized evidence, including keys no longer present in source."""
    after = None
    while True:
        body = {'size': 1000, 'query': query,
                'sort': [{'bucket_start_ms': 'asc'}, {'device': 'asc'}]}
        if after is not None:
            body['search_after'] = after
        hits = client.request('POST', client.target + '/_search', body)['hits']['hits']
        for hit in hits:
            doc = hit['_source']
            yield doc['bucket_start_ms'], doc['device']
        if len(hits) < 1000:
            return
        after = hits[-1]['sort']


def materializations(client, dirty, upper, config):
    # A positive-only composite has no row for a key's final removal. Make absence an
    # explicit durable delivery obligation, retaining ordering just like nonempty rows.
    positive = dict(rows(client, dirty, upper, config))
    result = []
    for start, device in sorted(dirty):
        identifier = f'{client.transform}#{start}:{device}'
        doc = positive.get(identifier, {'bucket_start_ms': start, 'device': device,
                'doc_count': 0, 'm1_sum': 0.0, 'm1_count': 0, 'm1_avg': None})
        # Homogeneous numeric types also matter inside dynamic metadata arrays.
        doc.update(m1_sum=float(doc['m1_sum']), m1_count=int(doc['m1_count']),
                   m1_avg=None if doc['m1_avg'] is None else float(doc['m1_avg']))
        result.append({'id': identifier, 'doc': doc})
    return result


def discover(client, config, previous, captured, upper):
    dirty = set()
    old_upper = previous['selection_upper_bound_ms']
    window = config['window_minutes'] * 60000
    lower = upper - window
    interval = config['bucket_minutes'] * 60000
    if upper > old_upper:
        dirty.update(keys(client, {'range': {'ts': {'gt': old_upper, 'lte': upper}}}, config))
        # Expiration evidence must come from target, not source: archival can already
        # have removed the old source keys. Include the partially expired edge hour.
        query = {'bool': {'filter': [
            {'range': {'bucket_start_ms': {'gte': (old_upper-window)//interval*interval,
                                            'lte': lower//interval*interval}}},
            {'range': {'doc_count': {'gt': 0}}}]}}
        dirty.update(target_keys(client, query))
    for shard, checkpoint in captured.items():
        saved = previous['shard_checkpoints'].get(shard, -1)
        if checkpoint > saved:
            # Revocations are changes even though they no longer match selected().
            query = {'bool': {'filter': [
                {'range': {'_seq_no': {'gt': saved, 'lte': checkpoint}}},
                {'range': {'ts': {'gt': lower, 'lte': old_upper}}}]}}
            dirty.update(keys(client, query, config, preference='_shards:' + shard))
    return materializations(client, dirty, upper, config)


def drain(client, path, record, credit):
    work = record['work']
    while work['rows']:
        if credit <= 0:
            raise RetryDispatch()
        batch = work['rows'][:min(credit, 8)]
        result = client.write_buckets([(row['id'], row['doc']) for row in batch])
        items = result.get('items', [])
        if len(items) != len(batch):
            raise RuntimeError('Incomplete bulk acknowledgement')
        remaining = []
        fatal = []
        accepted = 0
        for row, item in zip(batch, items):
            outcome = next(iter(item.values()))
            status = outcome['status']
            if 200 <= status < 300:
                accepted += 1
            else:
                remaining.append(row)
                if status == 409:
                    raise RetryDispatch()
                if status != 429:
                    fatal.append(outcome)
        # Persist all acknowledged successes, including non-prefix successes. A crash before
        # this write merely repeats a stable-id overwrite; it never advances committed bounds.
        work['rows'] = remaining + work['rows'][len(batch):]
        save(client, path, record)
        credit -= accepted
        if fatal:
            raise RuntimeError(f'Target failure: {fatal}')
        if remaining:
            raise RetryDispatch()
    client.request('POST', client.target + '/_refresh')
    committed = {key: value for key, value in record.items() if key != 'work'}
    committed.update(shard_checkpoints=work['shard_checkpoints'],
                     selection_upper_bound_ms=work['upper'], status='committed')
    save(client, path, committed)
    return committed, credit


def run_once(now_ms):
    config = json.loads(Path(__file__).with_name('transform.json').read_text())
    config['page_size'] = 1000
    client = OwnedClient()
    path = client.meta + '/_doc/' + client.transform
    response = client.request('GET', path)
    captured = client.checkpoints()
    record = client.claim(path, response)
    upper = max(now_ms - config['lag_minutes'] * 60000,
                record['selection_upper_bound_ms'], record.get('work', {}).get('upper', -1))
    credit = 8
    if 'work' in record:
        record, credit = drain(client, path, record, credit)
    # Capture belongs to this invocation, while any recovered work has its own earlier pair.
    # Reconcile that gap before declaring the supplied clock and write snapshot committed.
    if (record['shard_checkpoints'] != captured or
            record['selection_upper_bound_ms'] != upper):
        documents = discover(client, config, record, captured, upper)
        record = {**record, 'status': 'pending', 'work': {
            'shard_checkpoints': captured, 'upper': upper, 'rows': documents}}
        save(client, path, record)
        record, credit = drain(client, path, record, credit)


def main():
    parser = argparse.ArgumentParser(description='Run a continuous hourly materialization')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run_once')
    run.add_argument('--now-ms', type=int, required=True)
    args = parser.parse_args()
    try:
        run_once(args.now_ms)
    except RetryDispatch:
        raise SystemExit(75)
    except urllib.error.HTTPError as error:
        if error.code in (409, 429):
            raise SystemExit(75)
        raise


if __name__ == '__main__':
    main()
