"""Ledger delivery process. See README.md for its service contract."""
import argparse
import json
import signal
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import urllib.request
from confluent_kafka import Consumer, TopicPartition


def arguments():
    p = argparse.ArgumentParser()
    p.add_argument('--worker-id', required=True)
    p.add_argument('--bootstrap', required=True)
    p.add_argument('--group', required=True)
    p.add_argument('--topic', required=True)
    p.add_argument('--effects-url', required=True)
    return p.parse_args()


def consumer_config(a):
    return {
        'bootstrap.servers': a.bootstrap, 'group.id': a.group,
        'client.id': a.worker_id, 'group.protocol': 'classic',
        'partition.assignment.strategy': 'cooperative-sticky',
        'enable.auto.commit': False, 'enable.auto.offset.store': False,
        'auto.offset.reset': 'earliest', 'heartbeat.interval.ms': 1000,
        'session.timeout.ms': 10000, 'max.poll.interval.ms': 120000,
        'fetch.wait.max.ms': 100,
    }


def apply(a, member, msg):
    body = json.dumps({'worker_id': a.worker_id, 'member_id': member,
                       'partition': msg.partition(), 'offset': msg.offset()}).encode()
    req = urllib.request.Request(a.effects_url + '/apply', body,
                                 {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status != 200:
            raise RuntimeError('ledger service rejected application')


class Worker:
    WINDOW = 8

    def __init__(self, a):
        self.a = a
        self.c = Consumer(consumer_config(a))
        self.pool = ThreadPoolExecutor(max_workers=48)
        self.jobs = {}
        self.paused = set()
        self.running = True

    def assigned(self, client, parts):
        client.incremental_assign(parts)
        print('READY', self.a.worker_id, client.memberid(), flush=True)

    def stop(self, *_):
        self.running = False

    def collect(self):
        for key, jobs in list(self.jobs.items()):
            done = [(offset, f) for offset, f in jobs.items() if f.done()]
            if done:
                for offset, f in done:
                    f.result()
                    del jobs[offset]
                offset = max(offset for offset, _ in done) + 1
                self.c.commit(offsets=[TopicPartition(*key, offset)], asynchronous=False)
            if len(jobs) < self.WINDOW and key in self.paused and self.running:
                self.c.resume([TopicPartition(*key)])
                self.paused.remove(key)

    def run(self):
        self.c.subscribe([self.a.topic], on_assign=self.assigned)
        try:
            while self.running:
                self.collect()
                msg = self.c.poll(0.02)
                if msg is None:
                    continue
                if msg.error():
                    raise RuntimeError(msg.error())
                key = msg.topic(), msg.partition()
                jobs = self.jobs.setdefault(key, {})
                jobs[msg.offset()] = self.pool.submit(apply, self.a, self.c.memberid(), msg)
                if len(jobs) >= self.WINDOW:
                    self.c.pause([TopicPartition(*key)])
                    self.paused.add(key)
        finally:
            self.pool.shutdown(wait=True)
            self.collect()
            self.c.close()


def main():
    a = arguments()
    w = Worker(a)
    signal.signal(signal.SIGTERM, w.stop)
    signal.signal(signal.SIGINT, w.stop)
    w.run()


if __name__ == '__main__':
    main()
