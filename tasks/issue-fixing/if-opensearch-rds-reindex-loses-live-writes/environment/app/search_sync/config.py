"""Runtime configuration, from the environment the deployment provides to both processes."""

import os

ALIAS = os.environ.get("SEARCH_ALIAS", "products")


def opensearch_url() -> str:
    return os.environ["OPENSEARCH_URL"].rstrip("/")


def indexer_dsn() -> str:
    """The catalog_indexer role: owns schema reindex_aux, no access to the catalog tables."""
    return os.environ["INDEXER_DSN"]


def export_url() -> str:
    return os.environ["CATALOG_EXPORT_URL"].rstrip("/")
