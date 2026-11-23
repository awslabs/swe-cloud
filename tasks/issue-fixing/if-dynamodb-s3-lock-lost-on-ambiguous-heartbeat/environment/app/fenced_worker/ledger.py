"""The ledger object: s3://<bucket>/<key>, a JSON document

    {"fence": <highest epoch that has written>, "entries": [{"writer", "fence", "n"}, ...]}

kept in a versioned bucket.  Readers replay ``entries`` in order.
"""

import json
import logging

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger("fenced_worker.ledger")


class Ledger:
    def __init__(self, endpoint, region, bucket, key):
        self.endpoint = endpoint
        self.region = region
        self.bucket = bucket
        self.key = key
        self._s3 = self._new_client()

    def _new_client(self):
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=self.region,
            config=Config(
                s3={"addressing_style": "path"},
                connect_timeout=2,
                read_timeout=15,
                retries={"mode": "standard", "max_attempts": 3},
            ),
        )

    def reset(self):
        # Drop the connection pool. The local emulator can answer the next
        # request on a keep-alive connection with a bogus 400/405 after it
        # rejected one, so never reuse a connection after an S3 error.
        self._s3 = self._new_client()

    def read(self):
        resp = self._s3.get_object(Bucket=self.bucket, Key=self.key)
        body = json.loads(resp["Body"].read())
        return body, resp["ETag"]

    def write(self, body):
        resp = self._s3.put_object(
            Bucket=self.bucket,
            Key=self.key,
            Body=json.dumps(body, separators=(",", ":")).encode(),
            ContentType="application/json",
        )
        return resp["ETag"]

    def append(self, entry, fence):
        """Append one entry; returns the ETag of the version written."""
        try:
            body, _etag = self.read()
            body.setdefault("entries", []).append(entry)
            body["fence"] = fence
            return self.write(body)
        except (ClientError, BotoCoreError):
            self.reset()
            raise
