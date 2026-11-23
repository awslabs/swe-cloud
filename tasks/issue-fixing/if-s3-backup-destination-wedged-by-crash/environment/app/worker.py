import argparse
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from storage import Store, location


def now_ms():
    return int(time.time() * 1000)


class Busy(Exception):
    pass


class Lost(Exception):
    pass


class Backup:
    def __init__(self, args, owner):
        self.args, self.owner = args, owner
        bucket, self.prefix = location(args.dest)
        self.store = Store(args.endpoint_url, bucket)
        self.key = self.prefix + '.lock'
        self.stop = threading.Event()
        self.mutex = threading.Lock()
        self.error = None

    def open(self):
        body, _ = self.store.record(self.key)
        if body['state'] != 'free':
            raise Busy('destination occupied')
        self.epoch = body['epoch'] + 1
        body.update(state='held', owner=self.owner, epoch=self.epoch,
                    expires_at_ms=now_ms() + self.args.lease_ms, manifest_key=None)
        self.store.put_record(self.key, body)
        self.check()

    def check(self):
        body, _ = self.store.record(self.key)
        if body['owner'] != self.owner or body['state'] != 'held':
            raise Lost('destination owner changed')
        return body

    def renew(self):
        while not self.stop.wait(self.args.renew_ms / 1000):
            try:
                with self.mutex:
                    body = self.check()
                    body['expires_at_ms'] = now_ms() + self.args.lease_ms
                    self.store.put_record(self.key, body)
            except Exception as exc:
                self.error = exc
                self.stop.set()

    def upload(self):
        self.check()
        entries = []
        root = Path(self.args.source)
        for path in sorted(root.rglob('*')):
            if not path.is_file():
                continue
            if self.error:
                raise self.error
            name = path.relative_to(root).as_posix()
            data = path.read_bytes()
            key = self.prefix + 'data/' + name
            self.store.put(key, data)
            entries.append(dict(name=name, key=key, size=len(data),
                                sha256=hashlib.sha256(data).hexdigest()))
        return entries

    def finish(self, entries):
        body = self.check()
        manifest_key = self.prefix + 'manifests/' + self.owner + '.json'
        self.store.put_record(manifest_key, dict(backup_uuid=self.owner,
                              job_id=self.args.job_id, entries=entries))
        body.update(state='committed', manifest_key=manifest_key)
        self.store.put_record(self.key, body)

    def run(self):
        self.open()
        heartbeat = threading.Thread(target=self.renew, daemon=True)
        heartbeat.start()
        try:
            entries = self.upload()
        finally:
            self.stop.set()
            heartbeat.join()
        if self.error:
            raise self.error
        self.finish(entries)


def main():
    parser = argparse.ArgumentParser(description='Back up a directory snapshot to S3')
    parser.add_argument('--dest', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--endpoint-url', required=True)
    parser.add_argument('--lease-ms', type=int, default=3000)
    parser.add_argument('--renew-ms', type=int, default=750)
    args = parser.parse_args()
    owner = str(uuid.uuid4())
    print(json.dumps(dict(event='started', backup_uuid=owner)), flush=True)
    status, code, detail = 'BACKUP_CREATED', 0, ''
    try:
        Backup(args, owner).run()
    except Busy as exc:
        status, code, detail = 'BACKUP_ALREADY_EXISTS', 3, str(exc)
    except Exception as exc:
        status, code, detail = 'FAILED', 1, str(exc)
    print(json.dumps(dict(event='result', backup_uuid=owner, status=status, detail=detail)), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
