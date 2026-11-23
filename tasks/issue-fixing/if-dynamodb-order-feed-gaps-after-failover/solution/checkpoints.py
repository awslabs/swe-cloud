"""DynamoDB checkpoint partition access."""


import uuid
import time

from botocore.exceptions import ClientError

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
        while True:
            rows = self.load()
            prior = rows.get(self.CONTROL)
            rendezvous("loaded", owner=self.owner)
            if prior and "Mode" in prior:
                item = dict(prior)
            else:
                established = any(k != self.CONTROL for k in rows)
                chosen = "oldest" if established else mode
                item = {"StreamArn": {"S": self.stream},
                        "ShardID": {"S": self.CONTROL}, "Mode": {"S": chosen}}
                if chosen == "latest":
                    item["Cutoff"] = {"N": str(int(time.time()))}
            item["Owner"] = {"S": self.owner}
            condition = {"ConditionExpression": "attribute_not_exists(StreamArn)"}
            if prior:
                condition = {
                    "ConditionExpression": "#owner = :previous",
                    "ExpressionAttributeNames": {"#owner": "Owner"},
                    "ExpressionAttributeValues": {":previous": prior["Owner"]},
                }
            try:
                self.client.put_item(TableName=self.table, Item=item, **condition)
            except ClientError as error:
                if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    continue
                raise
            rows = self.load()
            self.current()
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
        rendezvous("checkpoint", owner=self.owner, shard=shard, sequence=sequence)
        try:
            self.client.transact_write_items(TransactItems=[
                {"ConditionCheck": {
                    "TableName": self.table,
                    "Key": {"StreamArn": {"S": self.stream},
                            "ShardID": {"S": self.CONTROL}},
                    "ConditionExpression": "#owner = :owner",
                    "ExpressionAttributeNames": {"#owner": "Owner"},
                    "ExpressionAttributeValues": {":owner": {"S": self.owner}},
                }},
                {"Put": {
                    "TableName": self.table,
                    "Item": {"StreamArn": {"S": self.stream},
                             "ShardID": {"S": shard},
                             "SequenceNumber": {"S": sequence}},
                }},
            ])
        except ClientError as error:
            if error.response["Error"]["Code"] == "TransactionCanceledException":
                self.current()
            raise
