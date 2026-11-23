import argparse
import hashlib
import json
from storage import Store, location

p = argparse.ArgumentParser(description='Verify the published backup')
p.add_argument('dest')
p.add_argument('--endpoint-url', required=True)
a = p.parse_args()
bucket, prefix = location(a.dest)
s = Store(a.endpoint_url, bucket)
record, _ = s.record(prefix + '.lock')
assert record['state'] == 'committed', 'no published backup'
manifest, _ = s.record(record['manifest_key'])
assert manifest['backup_uuid'] == record['owner'], 'wrong manifest'
for entry in manifest['entries']:
    data, _ = s.read(entry['key'])
    assert len(data) == entry['size'], entry['name']
    assert hashlib.sha256(data).hexdigest() == entry['sha256'], entry['name']
print(json.dumps(dict(backup_uuid=record['owner'], entries=len(manifest['entries']))))
