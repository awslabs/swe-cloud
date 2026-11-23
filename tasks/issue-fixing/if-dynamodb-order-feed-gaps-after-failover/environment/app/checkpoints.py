"""DynamoDB checkpoint partition access."""


import uuid

from coordination import rendezvous


class Superseded(Exception):
    pass


class Checkpoints:
    CONTROL = "__worker__"
    def __init__(self, client, table, stream):
        self.client = client
        self.table = table
        self.stream = stream
        self.owner = uuid.uuid4().hex

    def query(self, **extra):
        return self.client.query(
            TableName=self.table,
            KeyConditionExpression="StreamArn = :s",
            ExpressionAttributeValues={":s": {"S": self.stream}},
            ConsistentRead=True,
            **extra,
        )

    def has_any_state(self):
        return any(key != self.CONTROL for key in self.load())

    def load(self):
        rows = []
        page = self.query()
        while True:
            rows.extend(page.get("Items", []))
            key = page.get("LastEvaluatedKey")
            if not key:
                return {row["ShardID"]["S"]: row for row in rows}
            page = self.query(ExclusiveStartKey=key)

    def attach(self, mode):
        rows = self.load()
        rendezvous("loaded", owner=self.owner)
        self.client.put_item(
            TableName=self.table,
            Item={"StreamArn": {"S": self.stream},
                  "ShardID": {"S": self.CONTROL}, "Owner": {"S": self.owner}},
        )
        return rows

    def current(self):
        row = self.client.get_item(
            TableName=self.table,
            Key={"StreamArn": {"S": self.stream}, "ShardID": {"S": self.CONTROL}},
            ConsistentRead=True,
        ).get("Item", {})
        if row.get("Owner", {}).get("S") != self.owner:
            raise Superseded()

    def save(self, shard, sequence):
        self.current()
        rendezvous("checkpoint", owner=self.owner, shard=shard, sequence=sequence)
        self.client.put_item(
            TableName=self.table,
            Item={
                "StreamArn": {"S": self.stream},
                "ShardID": {"S": shard},
                "SequenceNumber": {"S": sequence},
            },
        )
