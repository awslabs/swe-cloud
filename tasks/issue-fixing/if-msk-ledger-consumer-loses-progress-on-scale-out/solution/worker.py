"""Ledger delivery process. See README.md for its service contract."""
import argparse
import json
import signal
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import urllib.request
from confluent_kafka import Consumer, TopicPartition, KafkaException


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


class Lane:
    def __init__(self):
        # Delivery order, rather than completion order, defines the safe prefix.
        self.jobs = deque()
        self.pending = None
        self.paused = False
        self.revoking = False


class Worker:
    WINDOW = 8

    def __init__(self, a):
        self.a = a
        self.c = Consumer(consumer_config(a))
        self.pool = ThreadPoolExecutor(max_workers=48)
        self.lanes = {}
        self.running = True
        self.retry_at = 0

    def stop(self, *_):
        self.running = False

    def assigned(self, client, parts):
        client.incremental_assign(parts)
        for tp in parts:
            self.lanes.setdefault((tp.topic, tp.partition), Lane())
        print('READY', self.a.worker_id, client.memberid(), flush=True)

    def collect(self):
        advanced = False
        for key, lane in self.lanes.items():
            while lane.jobs and lane.jobs[0][1].done():
                offset, future = lane.jobs.popleft()
                future.result()
                lane.pending = offset + 1
                advanced = True
            if (lane.paused and len(lane.jobs) < self.WINDOW
                    and self.running and not lane.revoking):
                self.c.resume([TopicPartition(*key)])
                lane.paused = False
        return advanced

    def flush(self):
        offsets = [TopicPartition(*key, lane.pending)
                   for key, lane in self.lanes.items() if lane.pending is not None]
        if not offsets:
            return
        try:
            result = self.c.commit(offsets=offsets, asynchronous=False)
        except KafkaException as e:
            if e.args[0].code() not in (22, 27):
                raise
        else:
            for tp in result or []:
                if tp.error:
                    if tp.error.code() not in (22, 27):
                        raise KafkaException(tp.error)
                else:
                    lane = self.lanes[(tp.topic, tp.partition)]
                    if lane.pending == tp.offset:
                        lane.pending = None
        self.retry_at = time.monotonic() + .1

    def service(self):
        advanced = self.collect()
        if advanced or time.monotonic() >= self.retry_at:
            self.flush()

    def revoked(self, client, parts):
        keys = {(tp.topic, tp.partition) for tp in parts}
        for key in keys:
            self.lanes[key].revoking = True
        # No more dispatch on these lanes. Retain every dispatched operation,
        # including HTTP calls not yet applied when this callback begins.
        while any(self.lanes[key].jobs or self.lanes[key].pending is not None
                  for key in keys):
            self.service()
            time.sleep(.01)
        client.incremental_unassign(parts)
        for key in keys:
            del self.lanes[key]

    def lost(self, client, parts):
        raise RuntimeError('unexpected ownership loss with irreversible effects')

    def dispatch(self, msg):
        key = msg.topic(), msg.partition()
        lane = self.lanes[key]
        future = self.pool.submit(apply, self.a, self.c.memberid(), msg)
        lane.jobs.append((msg.offset(), future))
        if len(lane.jobs) >= self.WINDOW:
            self.c.pause([TopicPartition(*key)])
            lane.paused = True

    def run(self):
        self.c.subscribe([self.a.topic], on_assign=self.assigned,
                         on_revoke=self.revoked, on_lost=self.lost)
        try:
            while self.running:
                self.service()
                msg = self.c.poll(.02)
                if msg is not None and self.running:
                    if msg.error():
                        raise RuntimeError(msg.error())
                    self.dispatch(msg)
            self.c.pause(self.c.assignment())
            # A graceful shutdown stops admission, not outstanding HTTP calls.
            while any(lane.jobs or lane.pending is not None for lane in self.lanes.values()):
                self.service()
                self.c.poll(.02)
        finally:
            try:
                self.c.close()
            finally:
                self.pool.shutdown(wait=True)


def main():
    a = arguments()
    w = Worker(a)
    signal.signal(signal.SIGTERM, w.stop)
    signal.signal(signal.SIGINT, w.stop)
    w.run()


if __name__ == '__main__':
    main()
