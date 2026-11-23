"""The DynamoDB version index.

Table layout (see docs/PROTOCOL.md): HASH `base_uri` (S), RANGE `version` (N); each row carries
`path` (S), `size` (N), `committer` (S) and, optionally, `generation` (S). All reads are strongly
consistent.
"""

from dataclasses import dataclass
from typing import Optional

from botocore.exceptions import BotoCoreError, ClientError

from .errors import IndexWriteFailed, NotFound, StrataError


@dataclass
class ManifestLocation:
    version: int
    path: str
    size: Optional[int]
    # Optional object generation (S3 VersionId), used for integrity checks. Manifests should be
    # immutable, so if we detect a change in the generation, it means the manifest was tampered
    # with. This might happen if the dataset was deleted and then re-created.
    generation: Optional[str]


def _location_from_item(item, version=None):
    path = item.get("path", {}).get("S")
    if path is None:
        raise NotFound("index row has no string attribute `path`")
    size = item.get("size", {}).get("N")
    generation = item.get("generation", {}).get("S")
    if version is None:
        version = int(item["version"]["N"])
    return ManifestLocation(
        version=version,
        path=path,
        size=int(size) if size is not None else None,
        generation=generation,
    )


class DynamoDBIndex:
    def __init__(self, client, table_name, committer_name):
        self.client = client
        self.table_name = table_name
        self.committer_name = committer_name
        self._check_schema()

    def _check_schema(self):
        try:
            table = self.client.describe_table(TableName=self.table_name)["Table"]
        except (ClientError, BotoCoreError) as error:
            raise StrataError(f"dynamodb table {self.table_name} is not usable: {error}") from error
        keys = {(k["KeyType"], k["AttributeName"]) for k in table.get("KeySchema", [])}
        if keys != {("HASH", "base_uri"), ("RANGE", "version")}:
            raise StrataError(
                f"dynamodb table {self.table_name} must have HASH and RANGE keys, "
                "named `base_uri` and `version` respectively"
            )

    @staticmethod
    def _key(base_uri, version):
        return {"base_uri": {"S": base_uri}, "version": {"N": str(version)}}

    def get_manifest_location(self, base_uri, version):
        try:
            response = self.client.get_item(
                TableName=self.table_name, Key=self._key(base_uri, version), ConsistentRead=True
            )
        except (ClientError, BotoCoreError) as error:
            raise StrataError(f"dynamodb get_item failed: {error}") from error
        item = response.get("Item")
        if item is None:
            raise NotFound(f"dynamodb not found: base_uri: {base_uri}; version: {version}")
        return _location_from_item(item, version)

    def get_latest_manifest_location(self, base_uri):
        try:
            response = self.client.query(
                TableName=self.table_name,
                KeyConditionExpression="base_uri = :base_uri",
                ExpressionAttributeValues={":base_uri": {"S": base_uri}},
                ScanIndexForward=False,
                Limit=1,
                ConsistentRead=True,
            )
        except (ClientError, BotoCoreError) as error:
            raise StrataError(f"dynamodb query failed: {error}") from error
        items = response.get("Items", [])
        if not items:
            return None
        if len(items) > 1:
            raise StrataError(f"dynamodb table {self.table_name} returned unexpected number of items")
        return _location_from_item(items[0])

    def _put(self, base_uri, version, path, size, generation, condition):
        item = {
            **self._key(base_uri, version),
            "path": {"S": path},
            "committer": {"S": self.committer_name},
            "size": {"N": str(size)},
        }
        if generation is not None:
            item["generation"] = {"S": generation}
        try:
            self.client.put_item(TableName=self.table_name, Item=item, ConditionExpression=condition)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            raise IndexWriteFailed(
                f"dynamodb put_item for {base_uri}@{version} failed: {error}",
                condition_failed=code == "ConditionalCheckFailedException",
            ) from error
        except BotoCoreError as error:
            raise IndexWriteFailed(f"dynamodb put_item for {base_uri}@{version} failed: {error}") from error

    def put_if_not_exists(self, base_uri, version, path, size, generation):
        """Write the row; fail if the version already exists."""
        self._put(
            base_uri, version, path, size, generation,
            "attribute_not_exists(base_uri) AND attribute_not_exists(version)",
        )

    def put_if_exists(self, base_uri, version, path, size, generation):
        """Write the row; fail if the version does **not** already exist."""
        self._put(
            base_uri, version, path, size, generation,
            "attribute_exists(base_uri) AND attribute_exists(version)",
        )
