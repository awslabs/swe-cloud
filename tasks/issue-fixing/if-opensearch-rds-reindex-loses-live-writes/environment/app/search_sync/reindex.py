"""Full reindex behind the alias, without taking search down.

    bin/reindex --alias products --schema-version 2

Creates a fresh index <alias>_v<schema>_<timestamp> with the schema's mapping, copies the whole
catalog into it from the export API while the alias keeps serving the current index, then promotes
the new index by moving the alias in one atomic _aliases request. Prints the new index name last.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import config, documents, store
from .export_client import PAGE_LIMIT_MAX, CatalogExport
from .opensearch import OpenSearch, OpenSearchError


def new_index_name(alias: str, schema_version: int) -> str:
    now = time.time()
    return f"{alias}_v{schema_version}_{time.strftime('%Y%m%d%H%M%S', time.gmtime(now))}{int(now * 1000) % 1000:03d}"


def import_rows(search: OpenSearch, index: str, rows: list[dict]) -> None:
    actions = [({"index": {"_index": index, "_id": documents.doc_id(row["id"])}}, documents.document(row)) for row in rows]
    result = search.bulk(actions)
    if result.get("errors"):
        failed = [item for item in result["items"] if item[next(iter(item))].get("error")]
        raise OpenSearchError(207, failed[:5])


def reindex(alias: str, schema_version: int, batch_size: int) -> str:
    search = OpenSearch(config.opensearch_url())
    export = CatalogExport(config.export_url())
    conn = store.connect()
    store.ensure_schema(conn)

    index = new_index_name(alias, schema_version)
    search.create_index(index, documents.index_body(schema_version))
    run_id = store.start_run(conn, alias, index, schema_version)
    print(f"reindex: importing into {index}", file=sys.stderr, flush=True)

    copied = 0
    for rows in export.pages(limit=batch_size):
        import_rows(search, index, rows)
        copied += len(rows)
    print(f"reindex: imported {copied} products", file=sys.stderr, flush=True)

    search.refresh(index)
    search.swap_alias(alias, index)
    store.finish_run(conn, run_id, "promoted")
    print(f"reindex: {alias} -> {index}", file=sys.stderr, flush=True)
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="full reindex behind the search alias")
    parser.add_argument("--alias", default=config.ALIAS)
    parser.add_argument("--schema-version", type=int, required=True, choices=sorted(documents.PROPERTIES))
    parser.add_argument("--batch-size", type=int, default=PAGE_LIMIT_MAX)
    args = parser.parse_args(argv)
    print(reindex(args.alias, args.schema_version, args.batch_size), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
