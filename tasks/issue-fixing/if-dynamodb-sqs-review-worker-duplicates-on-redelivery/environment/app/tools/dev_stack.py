"""Create the compose-review queue and tables on the local endpoint.

    python3 /app/tools/dev_stack.py            # prints the env to export

Mirrors the deployment: a standard queue with a 6 s visibility timeout and no
redrive policy, the frontend-owned tables, and a stand-in for the user-review
service's inbound queue. Safe to run more than once.
"""
import boto3
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.cfn_deploy import deploy

QUEUE_NAME = "upload-parts"
USER_REVIEW_QUEUE_NAME = "user-review-uploads"
VISIBILITY_TIMEOUT_S = 6

TABLES = {
    "review_compose": [("req_id", "HASH")],
    "responses": [("req_id", "HASH"), ("part", "RANGE")],
    "reviews_store": [("review_id", "HASH")],
    "movie_reviews": [("movie_id", "HASH")],
}


def main():
    outputs = deploy(boto3.client("cloudformation"), "review-dev-stack",
                     Path(__file__).resolve().parents[1] / "infra/seed.template.json")
    url = outputs["UploadUrl"]
    user_review_url = outputs["UserReviewUrl"]
    print(f"export QUEUE_URL={url}")
    print(f"export USER_REVIEW_QUEUE_URL={user_review_url}")


if __name__ == "__main__":
    main()
