#!/usr/bin/env python3
"""Orders worker process. See OPERATIONS.md for the deployment contract."""
import argparse
import signal
import time
from confluent_kafka import Consumer, TopicPartition
from confluent_kafka.admin import AdminClient
from group import Group, HEARTBEAT_S
from wire import KafkaError
from pipeline import Pipeline


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
        self.offsets = {}
        self.join = True
        self.last_commit = self.last_heartbeat = 0

    def stop(self, *_): self.stopping = True

    def commit(self):
        if self.offsets:
            self.group.broker.commit(self.args.group, self.group.generation,
                                     self.group.member, self.offsets)
        self.last_commit = time.monotonic()

    def install(self, new):
        offsets = self.group.broker.offsets(self.args.group, self.args.topic,
                                           [p for _, p in new]) if new else {}
        self.offsets = {tp: self.offsets.get(tp, max(0, offsets.get(tp, 0))) for tp in new}
        self.consumer.assign([TopicPartition(t, p, self.offsets[t, p]) for t, p in sorted(new)])
        self.pipeline.install(self.offsets)
        self.group.owned = new

    def close(self):
        try: self.commit()
        except Exception as exc: print('final commit:', exc, flush=True)
        self.pipeline.close()
        self.consumer.close()

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        try:
            while not self.stopping:
                try:
                    if self.join:
                        new = self.group.join_sync()
                        lost = self.group.owned - new
                        if lost:
                            self.commit()
                            self.install(new)
                            self.group.revoked(lost, self.args.barrier_url)
                            self.join = True
                        else:
                            self.install(new)
                            self.join = False
                        self.last_heartbeat = time.monotonic()
                        continue
                    now = time.monotonic()
                    if now-self.last_heartbeat >= HEARTBEAT_S:
                        self.group.heartbeat()
                        self.last_heartbeat = now
                    self.pipeline.tick()
                    record = self.consumer.poll(0.1) if self.pipeline.can_accept() else None
                    if record is not None:
                        if record.error(): raise RuntimeError(record.error())
                        self.pipeline.submit(record)
                        self.offsets[(record.topic(), record.partition())] = record.offset()+1
                    if time.monotonic()-self.last_commit >= 1: self.commit()
                except KafkaError as exc:
                    if exc.code in (22, 25, 27):
                        if exc.code == 25: self.group.member = ''
                        self.join = True
                    else: raise
        finally:
            self.close()


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
