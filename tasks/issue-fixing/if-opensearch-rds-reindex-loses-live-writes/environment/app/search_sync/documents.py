"""What a product looks like in search, per schema version.

The document is built from the catalog row the app committed. Every schema version stores the same
projection; versions differ in what the mapping indexes. Schema 2 adds ``brand`` as a keyword field
for the brand facet (schema 1 indexes do not map it, so it is unsearchable there and documents
written before the brand column existed do not carry it).
"""

from __future__ import annotations

PROPERTIES = {
    1: {"name": {"type": "text"}, "price_cents": {"type": "integer"}},
    2: {"name": {"type": "text"}, "price_cents": {"type": "integer"}, "brand": {"type": "keyword"}},
}


def index_body(schema_version: int) -> dict:
    """Settings and mappings for a new index of the given schema version."""
    return {
        "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {
            "_meta": {"search_schema": schema_version},
            "dynamic": False,
            "properties": PROPERTIES[schema_version],
        },
    }


def document(row: dict) -> dict:
    """The search document for one catalog row."""
    return {"name": row["name"], "price_cents": int(row["price_cents"]), "brand": row["brand"]}


def doc_id(product_id: int) -> str:
    return str(int(product_id))
