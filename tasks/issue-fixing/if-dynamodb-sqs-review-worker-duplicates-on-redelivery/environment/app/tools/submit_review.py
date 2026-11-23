"""Submit one review the way the frontend does, then show what came of it.

    python3 /app/tools/submit_review.py [--movie-id M] [--wait 15]

UploadReq (counter 0 item, the movie's list if absent), then the five upload
parts on the queue, then polls the replies and the composed review, and peeks
at the user-review service's queue (without consuming it).
"""
import argparse
import json
import os
import random
import time
import uuid

import boto3

PARTS = ("unique_id", "text", "rating", "user_id", "movie_id")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--movie-id", default="tt0062622")
    ap.add_argument("--wait", type=float, default=15)
    args = ap.parse_args()
    ddb = boto3.client("dynamodb")
    sqs = boto3.client("sqs")
    queue_url = os.environ["QUEUE_URL"]
    t_compose = os.environ.get("TABLE_COMPOSE", "review_compose")
    t_responses = os.environ.get("TABLE_RESPONSES", "responses")
    t_reviews = os.environ.get("TABLE_REVIEWS", "reviews_store")
    t_movies = os.environ.get("TABLE_MOVIES", "movie_reviews")

    req_id = "req-" + uuid.uuid4().hex[:12]
    values = {
        "unique_id": "rv-" + uuid.uuid4().hex[:12],
        "text": "A slow, patient film that rewards attention.",
        "rating": str(random.randint(1, 10)),
        "user_id": "user-" + uuid.uuid4().hex[:6],
        "movie_id": args.movie_id,
    }
    ddb.put_item(TableName=t_compose, Item={"req_id": {"S": req_id}, "counter": {"N": "0"}})
    try:
        ddb.put_item(
            TableName=t_movies,
            Item={"movie_id": {"S": args.movie_id}, "review_ids": {"L": []}},
            ConditionExpression="attribute_not_exists(movie_id)",
        )
    except ddb.exceptions.ConditionalCheckFailedException:
        pass
    order = list(PARTS)
    random.shuffle(order)
    for part in order:
        body = {"req_id": req_id, "part": part, "value": values[part], "movie_id": args.movie_id}
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))
    print(f"submitted {req_id}: {' '.join(order)}")

    deadline = time.time() + args.wait
    replies = []
    while time.time() < deadline:
        replies = ddb.query(
            TableName=t_responses,
            KeyConditionExpression="req_id = :r",
            ExpressionAttributeValues={":r": {"S": req_id}},
            ConsistentRead=True,
        )["Items"]
        if len(replies) == len(PARTS):
            break
        time.sleep(0.5)
    for r in sorted(replies, key=lambda r: int(r["rank"]["N"])):
        print(f"  reply {r['part']['S']:<9} rank={r['rank']['N']} composed={r['composed']['BOOL']}")
    item = ddb.get_item(TableName=t_compose, Key={"req_id": {"S": req_id}}, ConsistentRead=True).get("Item")
    print(f"  review_compose: {'counter=' + item['counter']['N'] if item else 'cleaned up'}")
    review = ddb.get_item(
        TableName=t_reviews, Key={"review_id": {"S": values["unique_id"]}}, ConsistentRead=True
    ).get("Item")
    print(f"  reviews_store: {'present' if review else 'missing'}")
    movie = ddb.get_item(TableName=t_movies, Key={"movie_id": {"S": args.movie_id}}, ConsistentRead=True)["Item"]
    ids = [v["S"] for v in movie.get("review_ids", {}).get("L", [])]
    print(f"  movie_reviews[{args.movie_id}]: {ids.count(values['unique_id'])}x this review, {len(ids)} total")
    handed = 0
    user_review_url = os.environ.get("USER_REVIEW_QUEUE_URL")
    if user_review_url:
        out = sqs.receive_message(QueueUrl=user_review_url, MaxNumberOfMessages=10,
                                  VisibilityTimeout=0, WaitTimeSeconds=1)
        handed = sum(json.loads(m["Body"]).get("review_id") == values["unique_id"]
                     for m in out.get("Messages", []))
    print(f"  user-review queue: {handed} message(s) for this review among the first 10")


if __name__ == "__main__":
    main()
