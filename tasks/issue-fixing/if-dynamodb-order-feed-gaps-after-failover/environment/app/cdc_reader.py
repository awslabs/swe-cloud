"""Single-table order change feed process."""
import argparse
import json
import os
import queue
import signal
import sys
import threading
import time

import boto3
from botocore.config import Config

from checkpoints import Checkpoints, Superseded
from progress import Progress


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-table", required=True)
    parser.add_argument("--checkpoint-table", required=True)
    parser.add_argument("--start-from", choices=("latest", "oldest"), default="latest")
    parser.add_argument("--checkpoint-limit", type=int, default=1000)
    parser.add_argument("--endpoint-url", default=os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566"))
    parser.add_argument("--region", default=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    args = parser.parse_args()
    if args.checkpoint_limit < 1:
        parser.error("checkpoint-limit must be positive")
    return args


class Reader:
    def __init__(self, args):
        self.args = args
        self.stop = False
        self.commands = queue.Queue()
        config = Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 3})
        session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=args.region)
        self.db = session.client("dynamodb", endpoint_url=args.endpoint_url, config=config)
        self.streams = session.client("dynamodbstreams", endpoint_url=args.endpoint_url, config=config)
        self.iterators = {}
        self.buffer = []

    def connect(self):
        table = self.db.describe_table(TableName=self.args.source_table)["Table"]
        self.arn = table["LatestStreamArn"]
        self.store = Checkpoints(self.db, self.args.checkpoint_table, self.arn)
        self.honor_start_from = not self.store.has_any_state()
        rows = self.store.attach(self.args.start_from)
        positions = {shard: row["SequenceNumber"]["S"] for shard, row in rows.items()
                     if "SequenceNumber" in row}
        self.progress = Progress(self.store, self.args.checkpoint_limit, positions)
        shards = []
        request = {"StreamArn": self.arn}
        while True:
            page = self.streams.describe_stream(**request)["StreamDescription"]
            shards.extend(page.get("Shards", []))
            if not page.get("LastEvaluatedShardId"):
                break
            request["ExclusiveStartShardId"] = page["LastEvaluatedShardId"]
        for shard in shards:
            shard_id = shard["ShardId"]
            request = {"StreamArn": self.arn, "ShardId": shard_id}
            if shard_id in positions:
                request.update(ShardIteratorType="AFTER_SEQUENCE_NUMBER", SequenceNumber=positions[shard_id])
            elif self.args.start_from == "latest" and self.honor_start_from:
                request["ShardIteratorType"] = "LATEST"
            else:
                request["ShardIteratorType"] = "TRIM_HORIZON"
            iterator = self.streams.get_shard_iterator(**request)["ShardIterator"]
            page = self.streams.get_records(ShardIterator=iterator, Limit=100)
            self.iterators[shard_id] = page.get("NextShardIterator")
            self.buffer.extend((shard_id, r) for r in page.get("Records", []))
        if shards:
            self.honor_start_from = False

    def read_commands(self):
        for line in sys.stdin:
            self.commands.put(line.strip())

    def settle(self):
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            words = command.split()
            if len(words) == 2 and words[0] == "ACK":
                self.progress.ack(words[1])

    def deliver(self, shard, record):
        data = record["dynamodb"]
        sequence = data["SequenceNumber"]
        self.progress.track(shard, sequence)
        print("DELIVER " + json.dumps({"id": data["Keys"]["id"]["S"],
                                      "seq": sequence, "shard": shard}), flush=True)

    def run(self):
        self.connect()
        threading.Thread(target=self.read_commands, daemon=True).start()
        print("READY", flush=True)
        for shard, record in self.buffer:
            self.deliver(shard, record)
        while not self.stop:
            self.settle()
            for shard, iterator in list(self.iterators.items()):
                if not iterator or self.stop:
                    continue
                page = self.streams.get_records(ShardIterator=iterator, Limit=100)
                self.iterators[shard] = page.get("NextShardIterator")
                for record in page.get("Records", []):
                    self.deliver(shard, record)
            time.sleep(0.05)
        # Drain commands already sent by the downstream before the stop signal.
        until = time.monotonic() + 0.25
        while time.monotonic() < until:
            self.settle()
            time.sleep(0.01)
        self.progress.flush()


def main():
    reader = Reader(arguments())
    def stop(signum, frame):
        reader.stop = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        reader.run()
    except Superseded:
        pass


if __name__ == "__main__":
    main()
