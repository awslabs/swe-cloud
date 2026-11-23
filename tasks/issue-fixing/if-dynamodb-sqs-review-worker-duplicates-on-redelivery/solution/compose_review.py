"""ComposeReview: the five upload handlers and the compose step.

Ported from the Go ComposeReview service. The frontend has already written the
review_compose item for the request (counter 0) and the movie's movie_reviews
item. Each upload stores its own part on the item and bumps the counter; the
upload that completes the set composes the review.

A worker can die after any call, and its message is executed again from the
top by another worker while the other parts keep moving the counter. So an
upload commits its part, its increment, the rank that increment produced
(`rank_<part>`) and its reply in one conditional transaction against the
counter value it read; the completing upload's transaction also stores and
lists the review. A re-execution finds its rank on the item instead of
incrementing again, and finishes what that rank obliges it to do.

What cannot join the transaction is the hand-off to the user-review service.
The item is the only record that the hand-off is still owed, so it is removed
only after the hand-off has been sent: a missing item means the request is
finished and nothing is owed.

Two kinds of state were not written by this code. The pre-switch workers
recorded parts without a rank, and replied for each before deleting its
message, so a part on the item without its rank is an upload that has already
replied. Their compose cleaned up before the hand-off and the reply, so a
missing item can also mean a completing upload that was reclaimed after its
cleanup: no reply row for it, review stored and listed, hand-off and reply
still owed. And a resubmitted review is composed by two requests: the review id,
not the request, decides whether the review still has to be stored and listed.

Moderation removes listed reviews from their movie page, so a review missing
from the page is not evidence that listing is owed. The review is stored and
listed in one transaction (the pre-switch compose listed it before its cleanup),
so a stored review has been listed: it is never listed again. Every write a
resumed (hibernated) execution can still make is fenced by the transaction's
counter condition or is harmless to repeat (hand-off, cleanup).
"""
import json
import random
import time

from botocore.exceptions import ClientError

from settings import N_PARTS, PART_NAMES


def _contended(err):
    """The transaction lost to a concurrent writer: re-read and decide again."""
    code = err.response["Error"]["Code"]
    if code == "TransactionConflictException":
        return True
    if code != "TransactionCanceledException":
        return False
    codes = [r.get("Code", "None") for r in err.response.get("CancellationReasons", [])]
    return not codes or all(c in ("None", "ConditionalCheckFailed", "TransactionConflict") for c in codes)


class ComposeReview:
    def __init__(self, ddb, sqs, settings):
        self.ddb = ddb
        self.sqs = sqs
        self.s = settings

    def _item(self, req_id):
        return self.ddb.get_item(
            TableName=self.s.table_compose,
            Key={"req_id": {"S": req_id}},
            ConsistentRead=True,
        ).get("Item")

    def upload(self, req_id, part, value):
        """Record one part of the review. Returns the upload's reply, or None when the
        upload has already replied: the request has finished (each reply was committed
        with its increment), or the pre-switch workers recorded this part."""
        if part not in PART_NAMES:
            raise ValueError(f"unknown part {part!r}")
        rank_attr = f"rank_{part}"
        while True:
            item = self._item(req_id)
            if item is None:
                return self._finished(req_id, part, value)
            if rank_attr in item:
                rank = int(item[rank_attr]["N"])
                break
            if part in item:
                return None
            counter = int(item["counter"]["N"])
            rank = counter + 1
            if rank > N_PARTS:
                raise ValueError(f"review_compose item for {req_id} is already complete without {part}")
            item = {**item, part: {"S": value}}
            writes = [
                {"Update": {
                    "TableName": self.s.table_compose,
                    "Key": {"req_id": {"S": req_id}},
                    "UpdateExpression": "SET #part = :value, #counter = :rank, #rank = :rank",
                    "ConditionExpression": "#counter = :seen AND attribute_not_exists(#rank)",
                    "ExpressionAttributeNames": {"#part": part, "#counter": "counter", "#rank": rank_attr},
                    "ExpressionAttributeValues": {
                        ":value": {"S": value}, ":seen": {"N": str(counter)}, ":rank": {"N": str(rank)},
                    },
                }},
                {"Put": {"TableName": self.s.table_responses, "Item": self._reply_item(req_id, part, rank)}},
            ]
            if rank == N_PARTS:
                writes += self._compose_writes(req_id, item)
            # A condition that fails here (a sibling request listed the review meanwhile, or
            # another upload moved the counter) cancels the transaction: re-read and decide again.
            try:
                self.ddb.transact_write_items(TransactItems=writes)
                break
            except ClientError as err:
                if not _contended(err):
                    raise
                time.sleep(random.uniform(0.005, 0.03))
        composed = rank == N_PARTS
        if composed:
            self.upload_user_review(item)
            self.cleanup(req_id)
        return {"rank": rank, "composed": composed}

    def _finished(self, req_id, part, value):
        """The request has no item: it is finished. Every upload this code ran replied in the
        transaction of its increment; an upload without a reply was the pre-switch completing upload,
        reclaimed after its cleanup. Its hand-off and reply are still owed; the page is not touched."""
        reply = self.ddb.get_item(
            TableName=self.s.table_responses,
            Key={"req_id": {"S": req_id}, "part": {"S": part}},
            ConsistentRead=True,
        ).get("Item")
        if reply is not None:
            return None
        if part == "unique_id":
            review = self.ddb.get_item(
                TableName=self.s.table_reviews, Key={"review_id": {"S": value}}, ConsistentRead=True
            ).get("Item")
        else:
            review = self._review_stored_by(req_id)
        if review is None or review.get("req_id", {}).get("S") != req_id:
            raise LookupError(f"{req_id} has no review_compose item, no reply for {part} and no stored review")
        replies = self.ddb.query(
            TableName=self.s.table_responses,
            KeyConditionExpression="req_id = :r",
            ExpressionAttributeValues={":r": {"S": req_id}},
            ConsistentRead=True,
        )["Items"]
        if any(int(r["rank"]["N"]) == N_PARTS for r in replies):
            raise ValueError(f"{req_id}: {part} has no reply but another upload replied rank {N_PARTS}")
        self.upload_user_review(review)
        self.ddb.put_item(TableName=self.s.table_responses, Item=self._reply_item(req_id, part, N_PARTS))
        return {"rank": N_PARTS, "composed": True}

    def _review_stored_by(self, req_id):
        kwargs = {}
        while True:
            page = self.ddb.scan(
                TableName=self.s.table_reviews, ConsistentRead=True,
                FilterExpression="req_id = :r", ExpressionAttributeValues={":r": {"S": req_id}}, **kwargs,
            )
            if page.get("Items"):
                return page["Items"][0]
            if "LastEvaluatedKey" not in page:
                return None
            kwargs = {"ExclusiveStartKey": page["LastEvaluatedKey"]}

    def _reply_item(self, req_id, part, rank):
        return {
            "req_id": {"S": req_id},
            "part": {"S": part},
            "rank": {"N": str(rank)},
            "composed": {"BOOL": rank == N_PARTS},
        }

    def _compose_writes(self, req_id, item):
        """Store and list the review unless a request carrying the same review id already stored it
        (which listed it in the same transaction; if it is off the page now, moderation took it down)."""
        review = {p: item[p] for p in PART_NAMES}
        review_id = review["unique_id"]["S"]
        stored = self.ddb.get_item(
            TableName=self.s.table_reviews, Key={"review_id": {"S": review_id}}, ConsistentRead=True
        ).get("Item")
        if stored is not None:
            return []
        writes = [{"Put": {
            "TableName": self.s.table_reviews,
            "Item": {"review_id": {"S": review_id}, "req_id": {"S": req_id}, **review},
            "ConditionExpression": "attribute_not_exists(review_id)",
        }}]
        movie = self.ddb.get_item(
            TableName=self.s.table_movies, Key={"movie_id": review["movie_id"]}, ConsistentRead=True
        ).get("Item") or {}
        if {"S": review_id} not in movie.get("review_ids", {}).get("L", []):
            writes.append({"Update": {
                "TableName": self.s.table_movies,
                "Key": {"movie_id": review["movie_id"]},
                "UpdateExpression": "SET review_ids = list_append(if_not_exists(review_ids, :empty), :ids)",
                "ConditionExpression": "NOT contains(review_ids, :id)",
                "ExpressionAttributeValues": {
                    ":ids": {"L": [{"S": review_id}]}, ":empty": {"L": []}, ":id": {"S": review_id},
                },
            }})
        return writes

    def cleanup(self, req_id):
        self.ddb.delete_item(TableName=self.s.table_compose, Key={"req_id": {"S": req_id}})

    def upload_user_review(self, item):
        """Asynchronous call into the user-review service (UploadUserReview). Repeats are harmless."""
        self.sqs.send_message(
            QueueUrl=self.s.user_review_queue_url,
            MessageBody=json.dumps({
                "review_id": item["unique_id"]["S"],
                "user_id": item["user_id"]["S"],
                "movie_id": item["movie_id"]["S"],
                "rating": item["rating"]["S"],
            }),
        )
