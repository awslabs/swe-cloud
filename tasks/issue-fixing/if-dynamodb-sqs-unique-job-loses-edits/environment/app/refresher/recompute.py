"""Default refresh command: recompute one entity's derived data.

Reads the entity's current ``dirty_seq`` and, once the (simulated) recompute is done, records it as
``processed_seq``. Exit status 0 means the refresh succeeded.
"""

import argparse
import os
import sys
import time

from . import config


def main(argv=None):
    parser = argparse.ArgumentParser(prog="refresher.recompute")
    parser.add_argument("--entity", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    args = parser.parse_args(argv)

    table = config.load().entity_table
    client = config.dynamodb()
    item = client.get_item(TableName=table, Key={"pk": {"S": args.entity}},
                           ConsistentRead=True).get("Item", {})
    seen = int(item.get("dirty_seq", {}).get("N", "0"))
    time.sleep(int(os.environ.get("REFRESH_WORK_MS", "200")) / 1000)
    client.update_item(TableName=table, Key={"pk": {"S": args.entity}},
                       UpdateExpression="SET processed_seq = :seen, refreshed_at = :now",
                       ExpressionAttributeValues={":seen": {"N": str(seen)},
                                                  ":now": {"N": str(int(time.time()))}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
