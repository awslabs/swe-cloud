"""ComposeReview: the five upload handlers and the compose step.

Ported from the Go ComposeReview service. The frontend has already written the
review_compose item for the request (counter 0) and the movie's movie_reviews
item. Each upload stores its own part on the item and bumps the counter; the
upload that completes the set composes the review: it stores it, lists it on
the movie, cleans up the request's review_compose item and hands the review to
the user-review service.
"""
import json

from settings import N_PARTS, PART_NAMES


class ComposeReview:
    def __init__(self, ddb, sqs, settings):
        self.ddb = ddb
        self.sqs = sqs
        self.s = settings

    def upload(self, req_id, part, value):
        """Record one part of the review. Returns the upload's reply."""
        if part not in PART_NAMES:
            raise ValueError(f"unknown part {part!r}")
        self.ddb.update_item(
            TableName=self.s.table_compose,
            Key={"req_id": {"S": req_id}},
            UpdateExpression="SET #part = :value ADD #counter :one",
            ExpressionAttributeNames={"#part": part, "#counter": "counter"},
            ExpressionAttributeValues={":value": {"S": value}, ":one": {"N": "1"}},
        )
        return self.try_compose_and_upload(req_id)

    def try_compose_and_upload(self, req_id):
        item = self.ddb.get_item(
            TableName=self.s.table_compose,
            Key={"req_id": {"S": req_id}},
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            raise LookupError(f"review_compose item for {req_id} does not exist")
        if "counter" not in item:
            raise LookupError(f"review_compose item for {req_id} has no counter")
        counter = int(item["counter"]["N"])
        composed = False
        if counter == N_PARTS:
            self.compose(req_id, item)
            composed = True
        return {"rank": counter, "composed": composed}

    def compose(self, req_id, item):
        review = {p: item[p] for p in PART_NAMES if p in item}
        review_id = review["unique_id"]["S"]
        movie_id = review["movie_id"]["S"]
        self.ddb.put_item(
            TableName=self.s.table_reviews,
            Item={"review_id": {"S": review_id}, "req_id": {"S": req_id}, **review},
        )
        self.ddb.update_item(
            TableName=self.s.table_movies,
            Key={"movie_id": {"S": movie_id}},
            UpdateExpression="SET review_ids = list_append(if_not_exists(review_ids, :empty), :ids)",
            ExpressionAttributeValues={
                ":ids": {"L": [{"S": review_id}]},
                ":empty": {"L": []},
            },
        )
        self.cleanup(req_id)
        self.upload_user_review(review_id, review)

    def cleanup(self, req_id):
        self.ddb.delete_item(TableName=self.s.table_compose, Key={"req_id": {"S": req_id}})

    def upload_user_review(self, review_id, review):
        """Asynchronous call into the user-review service (UploadUserReview)."""
        self.sqs.send_message(
            QueueUrl=self.s.user_review_queue_url,
            MessageBody=json.dumps({
                "review_id": review_id,
                "user_id": review["user_id"]["S"],
                "movie_id": review["movie_id"]["S"],
                "rating": review["rating"]["S"],
            }),
        )

    def reply(self, req_id, part, result):
        self.ddb.put_item(
            TableName=self.s.table_responses,
            Item={
                "req_id": {"S": req_id},
                "part": {"S": part},
                "rank": {"N": str(result["rank"])},
                "composed": {"BOOL": bool(result["composed"])},
            },
        )
