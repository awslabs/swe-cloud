"""Entrypoint for the continuous materializer."""
import argparse
import json
import uuid
from pathlib import Path
from os_http import OpenSearch
from aggregation import selected, keys, materialize


def run_once(now_ms):
    config = json.loads(Path(__file__).with_name('transform.json').read_text())
    client = OpenSearch()
    meta_path = client.meta + '/_doc/' + client.transform
    previous = client.request('GET', meta_path)['_source']
    owner = uuid.uuid4().hex
    previous['owner'] = owner
    client.request('PUT', meta_path, previous)
    captured = client.checkpoints()
    upper = now_ms - config['lag_minutes'] * 60000
    dirty = set()
    for shard, checkpoint in captured.items():
        saved = previous['shard_checkpoints'].get(shard, -1)
        if checkpoint <= saved:
            continue
        query = {'bool': {'filter': [
            {'range': {'_seq_no': {'gt': saved, 'lte': checkpoint}}}, selected(upper)]}}
        dirty.update(keys(client, query, config, preference='_shards:' + shard))
    if client.request('GET', meta_path)['_source'].get('owner') != owner:
        raise SystemExit(75)
    materialize(client, dirty, upper, config)
    client.request('PUT', meta_path, {'shard_checkpoints': captured,
                   'selection_upper_bound_ms': now_ms, 'status': 'committed', 'owner': owner})


def main():
    parser = argparse.ArgumentParser(description='Run a continuous hourly materialization')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run_once')
    run.add_argument('--now-ms', type=int, required=True)
    args = parser.parse_args()
    run_once(args.now_ms)


if __name__ == '__main__':
    main()
