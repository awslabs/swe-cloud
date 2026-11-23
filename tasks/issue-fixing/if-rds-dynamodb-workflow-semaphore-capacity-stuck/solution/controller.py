#!/usr/bin/env python3
import argparse
import logging
import signal
import time
import threading
from botocore.exceptions import ClientError
from db import Database
from workflows import Workflows
from seam import pause

TERMINAL = {'Succeeded', 'Failed', 'Error'}
log = logging.getLogger('controller')


class Controller:
    def __init__(self, args):
        self.args = args
        self.db = Database(args.pg_dsn, args.name, args.inactive_controller_seconds)
        self.workflows = Workflows(args.dynamodb_endpoint, args.workflow_table)
        self.locks = set()
        self.fenced = set()
        self.next_heartbeat = 0
        self.next_existence = 0
        self.running = True

    def initialize(self):
        for item in self.workflows.list():
            if item['phase'] == 'Running':
                for lock in item.get('sync_holding', []):
                    self.locks.add('sem/' + lock)
                    if not self.db.has_hold('sem/' + lock, item['wf_key'], item['uid']):
                        self.workflows.update(item, {'phase': 'Failed', 'message': 'Recorded hold is no longer backed by database'})
                        break

    def reap(self, lock, key, uid):
        if self.workflows.close_execution(uid):
            self.db.release(lock, key, uid)

    def release_all(self, item):
        # SQL remains authoritative even if admission never persisted status.
        rows = self.db.query('SELECT name FROM sync_state WHERE controller=%s '
                             'AND workflowkey=%s AND execution_id=%s',
                             (self.args.name, item['wf_key'], item['uid']))
        for (lock,) in rows:
            self.reap(lock, item['wf_key'], item['uid'])

    def check_existence(self):
        rows = self.db.query(
            'SELECT name,workflowkey,execution_id FROM sync_state WHERE controller=%s',
            (self.args.name,))
        for lock, key, uid in rows:
            item = self.workflows.get(key)
            if item is None or item['uid'] != uid or item['phase'] in TERMINAL:
                self.reap(lock, key, uid)

    def process(self, item):
        phase = item['phase']
        if phase in TERMINAL:
            self.release_all(item)
        elif phase == 'Pending':
            self.locks.add('sem/' + item['semaphore'])
            if self.db.acquire(item):
                pause('after_commit', name=self.args.name, workflowkey=item['wf_key'],
                      semaphore=item['semaphore'])
                self.workflows.update(item, {'phase': 'Running', 'sync_holding': [item['semaphore']]})
        elif phase == 'Running' and item.get('exec_done'):
            self.workflows.update(item, {'phase': 'Succeeded'})

    def fence(self, items):
        # Disabling launches needs only receipts in the workflow API. Do it ahead of
        # any SQL so a database interruption cannot delay it; one full pass closes
        # every existing node and thereby freezes further expansion.
        for item in items:
            if item['phase'] in TERMINAL and item['uid'] not in self.fenced:
                try:
                    self.workflows.close_execution(item['uid'])
                    self.fenced.add(item['uid'])
                except Exception:
                    # A failed API call is no evidence; the next tick retries it.
                    log.exception('fencing %s failed', item['wf_key'])

    def tick(self):
        items = self.workflows.list()
        self.fence(items)
        for item in sorted(items, key=lambda w: (-int(w['priority']), w['created_at'])):
            try:
                self.process(item)
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise

    def maintenance(self):
        while self.running:
            try:
                if time.monotonic() >= self.next_heartbeat:
                    self.db.heartbeat()
                    self.next_heartbeat = time.monotonic() + self.args.heartbeat_seconds
                if time.monotonic() >= self.next_existence:
                    self.check_existence()
                    self.next_existence = time.monotonic() + 2
            except Exception:
                log.exception('maintenance failed')
            time.sleep(.25)

    def run(self):
        self.db.heartbeat()
        self.initialize()
        threading.Thread(target=self.maintenance, daemon=True).start()
        print('READY', flush=True)
        while self.running:
            try:
                self.tick()
            except Exception:
                log.exception('reconcile failed')
            time.sleep(.25)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--name', required=True)
    p.add_argument('--cluster', required=True)
    p.add_argument('--workflow-table', required=True)
    p.add_argument('--pg-dsn', required=True)
    p.add_argument('--dynamodb-endpoint', required=True)
    p.add_argument('--heartbeat-seconds', type=float, default=2)
    p.add_argument('--inactive-controller-seconds', type=float, default=15)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    controller = Controller(args)
    signal.signal(signal.SIGTERM, lambda *_: setattr(controller, 'running', False))
    controller.run()


if __name__ == '__main__':
    main()
