"""Ownership items in tgb_ownership: (owner=<cluster id>, target="ip:port"), one per target.

The highest binding generation this cluster has started to act on is kept in the same table, under
an owner key that is not a cluster id, so it survives restarts and is never mistaken for a target.
"""
from botocore.exceptions import ClientError


class OwnershipLedger:
    def __init__(self, dynamodb, table, owner):
        self.dynamodb = dynamodb
        self.table = table
        self.owner = owner
        self.mark_key = {"owner": {"S": f"{owner}#binding"}, "target": {"S": "generation"}}

    def generation_mark(self):
        item = self.dynamodb.get_item(TableName=self.table, Key=self.mark_key,
                                      ConsistentRead=True).get("Item")
        return int(item["generation"]["N"]) if item else None

    def advance_generation(self, generation):
        """Record `generation` as acted on. False if a higher one is already recorded."""
        try:
            self.dynamodb.update_item(
                TableName=self.table, Key=self.mark_key, UpdateExpression="SET #g = :g",
                ConditionExpression="attribute_not_exists(#g) OR #g <= :g",
                ExpressionAttributeNames={"#g": "generation"},
                ExpressionAttributeValues={":g": {"N": str(generation)}})
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def owned(self):
        found, kwargs = set(), {}
        while True:
            resp = self.dynamodb.query(
                TableName=self.table, KeyConditionExpression="#o = :o",
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":o": {"S": self.owner}},
                ConsistentRead=True, **kwargs)
            found.update(item["target"]["S"] for item in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return found
            kwargs = {"ExclusiveStartKey": resp["LastEvaluatedKey"]}

    def claimed_by_others(self, targets):
        """The subset of `targets` that another cluster's items name (its pods now hold those
        addresses)."""
        wanted, found, kwargs = set(targets), set(), {}
        while wanted:
            resp = self.dynamodb.scan(TableName=self.table, ConsistentRead=True, **kwargs)
            for item in resp.get("Items", []):
                if item["owner"]["S"] != self.owner and item["target"]["S"] in wanted:
                    found.add(item["target"]["S"])
            if "LastEvaluatedKey" not in resp:
                break
            kwargs = {"ExclusiveStartKey": resp["LastEvaluatedKey"]}
        return found

    def claim(self, targets):
        for target in sorted(targets):
            self.dynamodb.put_item(TableName=self.table,
                                   Item={"owner": {"S": self.owner}, "target": {"S": target}})

    def release(self, targets):
        for target in sorted(targets):
            self.dynamodb.delete_item(TableName=self.table,
                                      Key={"owner": {"S": self.owner}, "target": {"S": target}})
