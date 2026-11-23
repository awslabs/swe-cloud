#!/usr/bin/env python3
"""Orders worker process. See OPERATIONS.md for the deployment contract."""
import argparse
import signal
import threading
import time
import uuid
from confluent_kafka import Consumer, TopicPartition
from confluent_kafka.admin import AdminClient
from group import Group, HEARTBEAT_S
from pipeline import Pipeline
from wire import KafkaError, read_subscription, read_assignment, s, i


class Worker:
    def __init__(self, args):
        self.args = args
        self.stopping = False
        self.group = Group(args.bootstrap, args.group, args.instance_id, args.topic,
                           AdminClient({'bootstrap.servers': args.bootstrap}))
        self.consumer = Consumer({'bootstrap.servers': args.bootstrap, 'group.id': args.group,
                                  'enable.auto.commit': False, 'enable.auto.offset.store': False,
                                  'auto.offset.reset': 'earliest'})
        self.pipeline = Pipeline(args.bootstrap, args.output_topic, args.processor_url, args.instance_id)
        self.join = True
        self.missing_since = {}
        self.last_audit = self.last_commit = 0
        self.phase = 0
        self.heartbeat_stop = threading.Event()
        self.rejoin = threading.Event()
        self.heartbeat_error = None

    def stop(self, *_): self.stopping = True

    def heartbeats(self):
        while not self.heartbeat_stop.wait(HEARTBEAT_S / 2):
            phase = self.phase
            member, generation = self.group.member, self.group.generation
            if not member or generation < 0: continue
            try:
                body = s(self.args.group)+i(generation)+s(member)+s(self.args.instance_id)
                r = self.group.broker.rpc(12, 3, body, timeout=5)
                r.i(); code = r.h()
                if code: raise KafkaError(code)
            except Exception as exc:
                if phase != self.phase or phase % 2: continue
                if getattr(exc, 'code', None) in (22, 25, 27): self.rejoin.set()
                else: self.heartbeat_error = exc

    def commit(self):
        if self.pipeline.safe:
            self.group.broker.commit(self.args.group, self.group.generation,
                                     self.group.member, self.pipeline.safe)
        self.last_commit = time.monotonic()

    def maybe_commit(self):
        if time.monotonic()-self.last_commit >= 1: self.commit()

    def install(self, new):
        offsets = self.group.broker.offsets(self.args.group, self.args.topic,
                                           [p for _, p in new]) if new else {}
        starts = {tp: self.pipeline.safe.get(tp, max(0, offsets.get(tp, 0))) for tp in new}
        retained = self.group.owned & new
        positions = {(tp.topic, tp.partition): tp.offset for tp in
                     self.consumer.position([TopicPartition(t, p) for t, p in retained])}
        self.pipeline.install(starts)
        self.consumer.assign([TopicPartition(t, p, max(starts[t, p], positions.get((t, p), -1)))
                              for t, p in sorted(new)])
        self.group.owned = new

    def policy_tick(self):
        now = time.monotonic()
        if now-self.last_audit < 0.5: return
        self.last_audit = now
        view = self.group.broker.describe(self.args.group)
        if view['state'] != 'Stable' or view['protocol'] != 'cooperative-sticky':
            self.missing_since.clear()
            return
        expected, actual = set(), set()
        for member in view['members']:
            topics, _ = read_subscription(member['metadata'])
            for topic in topics:
                info = self.group.metadata.list_topics(topic, timeout=5).topics[topic]
                if info.error: raise RuntimeError(info.error)
                expected.update((topic, p) for p in info.partitions)
            actual.update(read_assignment(member['assignment']))
        missing = expected - actual
        self.missing_since = {tp: self.missing_since.get(tp, now) for tp in missing}
        if not any(now-start >= 10 for start in self.missing_since.values()): return
        elected = min(member['instance'] for member in view['members'])
        if self.args.instance_id != elected: return
        # The changed subscription requests a real generation without dropping
        # processing authority prematurely. join/install performs the handoff.
        self.group.user_data = uuid.uuid4().bytes
        self.missing_since.clear()
        self.join = True

    def notify_revoked(self, lost):
        done = threading.Event()
        errors = []
        def callback():
            try: self.group.revoked(lost, self.args.barrier_url)
            except Exception as exc: errors.append(exc)
            finally: done.set()
        thread = threading.Thread(target=callback, daemon=True)
        thread.start()
        while not done.wait(0.05):
            self.pipeline.tick()
            self.maybe_commit()
        thread.join()
        if errors: raise errors[0]

    def close(self):
        try:
            self.pipeline.drain(set(self.pipeline.safe), self.maybe_commit)
            self.commit()
        finally:
            self.pipeline.close()
            self.consumer.close()

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        thread = threading.Thread(target=self.heartbeats, daemon=True)
        thread.start()
        try:
            while not self.stopping:
                try:
                    if self.heartbeat_error: raise self.heartbeat_error
                    if self.rejoin.is_set(): self.join = True
                    if self.join:
                        self.phase += 1
                        try:
                            new = self.group.join_sync()
                            lost = self.group.owned - new
                            if lost:
                                self.pipeline.drain(lost, self.maybe_commit)
                                self.commit()
                                self.install(new)
                                self.notify_revoked(lost)
                                self.join = True
                            else:
                                self.install(new)
                                self.join = False
                            self.rejoin.clear()
                        finally:
                            self.phase += 1
                        continue
                    self.pipeline.tick()
                    self.maybe_commit()
                    self.policy_tick()
                    if self.join: continue
                    if not self.pipeline.can_accept():
                        time.sleep(0.01)
                        continue
                    record = self.consumer.poll(0.05)
                    if record is not None:
                        if record.error(): raise RuntimeError(record.error())
                        self.pipeline.submit(record)
                except KafkaError as exc:
                    if exc.code in (22, 25, 27):
                        if exc.code == 25: self.group.member = ''
                        self.join = True
                    else: raise
        finally:
            try: self.close()
            finally:
                self.heartbeat_stop.set()
                thread.join(6)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--bootstrap', required=True)
    p.add_argument('--group', required=True)
    p.add_argument('--instance-id', required=True)
    p.add_argument('--topic', required=True)
    p.add_argument('--barrier-url', default='')
    p.add_argument('--output-topic', required=True)
    p.add_argument('--processor-url', default='')
    Worker(p.parse_args()).run()


if __name__ == '__main__': main()
