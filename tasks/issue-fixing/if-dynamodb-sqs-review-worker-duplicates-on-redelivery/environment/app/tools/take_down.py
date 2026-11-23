"""Take a review down the way moderation's console does.

    python3 /app/tools/take_down.py <movie_id> <review_id>

Removes the review id from the movie's review_ids (conditionally on the
position it was read at, re-reading if the list moved). The reviews_store item
is left alone; the author's history keeps the review.
"""
import argparse
import os

import boto3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("movie_id")
    ap.add_argument("review_id")
    args = ap.parse_args()
    ddb = boto3.client("dynamodb")
    table = os.environ.get("TABLE_MOVIES", "movie_reviews")
    key = {"movie_id": {"S": args.movie_id}}
    while True:
        item = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item") or {}
        ids = [v["S"] for v in item.get("review_ids", {}).get("L", [])]
        if args.review_id not in ids:
            print(f"{args.review_id} is not on {args.movie_id}'s page")
            return
        i = ids.index(args.review_id)
        try:
            ddb.update_item(
                TableName=table, Key=key,
                UpdateExpression=f"REMOVE review_ids[{i}]",
                ConditionExpression=f"review_ids[{i}] = :id",
                ExpressionAttributeValues={":id": {"S": args.review_id}},
            )
        except ddb.exceptions.ConditionalCheckFailedException:
            continue
        print(f"took {args.review_id} down from {args.movie_id}")


if __name__ == "__main__":
    main()
