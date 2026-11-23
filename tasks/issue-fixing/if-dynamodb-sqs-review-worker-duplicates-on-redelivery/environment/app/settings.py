"""Runtime configuration for the compose-review workers, read from the environment."""
import os
from dataclasses import dataclass

N_PARTS = 5
PART_NAMES = ("unique_id", "text", "rating", "user_id", "movie_id")


@dataclass(frozen=True)
class Settings:
    queue_url: str
    user_review_queue_url: str
    table_compose: str
    table_responses: str
    table_reviews: str
    table_movies: str
    table_idempotency: str

    @classmethod
    def from_env(cls):
        return cls(
            queue_url=os.environ["QUEUE_URL"],
            user_review_queue_url=os.environ["USER_REVIEW_QUEUE_URL"],
            table_compose=os.environ.get("TABLE_COMPOSE", "review_compose"),
            table_responses=os.environ.get("TABLE_RESPONSES", "responses"),
            table_reviews=os.environ.get("TABLE_REVIEWS", "reviews_store"),
            table_movies=os.environ.get("TABLE_MOVIES", "movie_reviews"),
            table_idempotency=os.environ.get("TABLE_IDEMPOTENCY", "compose_idempotency"),
        )
