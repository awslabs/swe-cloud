# search-sync

Keeps the storefront's product search (OpenSearch) in step with the catalog (PostgreSQL `products`).
Search reads and writes go through the alias **`products`**; the concrete index behind it changes
when the document schema changes (`products_v1` today).

```
storefront app ──COMMIT──▶ catalog DB (products)
      │                         │
      └─POST /on_commit──▶ sync-server ──▶ OpenSearch  alias "products" ─▶ products_v1
                                              ▲
 bin/reindex ──GET /rows, /row/<id>──▶ export API (catalog team, read replica)
```

## Processes

| | what | runs |
|---|---|---|
| `bin/sync-server [--port 8702]` | the after-commit hook (`search_sync/server.py`) | always, several replicas behind the app |
| `bin/reindex --alias products --schema-version N` | full reindex into a fresh index, then alias swap (`search_sync/reindex.py`) | on demand, as a job on an ops node |

Both read `OPENSEARCH_URL`, `INDEXER_DSN` and `CATALOG_EXPORT_URL` from the environment. They run
on different hosts and share nothing but OpenSearch and the indexer's Postgres schema; neither can
see the other's memory or local files.

The indexer's database role (`catalog_indexer`) owns schema `reindex_aux` and nothing else. It has
no access to the catalog tables: the catalog is read through the export API
(`catalog/export_api.py`), whose docstring describes what a page is and is not.

## Contracts with the catalog team

- `catalog/writes.py` is how the app changes a product and calls the hook: one change per product
  at a time, in commit order; the hook gets 2 s, and a failed or timed-out call is dropped.
- `catalog/schema.sql` is the table. Ids are never reused. `updated_at` is display-only.
- Search is the storefront's product listing; merchants edit a product and look it up straight away,
  so a committed change must be visible through `products` within 2 seconds of the hook answering.

## Documents

`search_sync/documents.py`: the document for a row is `{name, price_cents, brand}` with `_id` = the
product id. Schema 2 maps `brand` as a keyword for the brand facet; schema 1 does not map it, and
documents indexed before the brand column existed do not carry it, which is why moving to schema 2
needs a full reindex from the catalog rather than a copy of `products_v1`.

## Reindexing

`bin/reindex` creates `<alias>_v<N>_<timestamp>`, copies the catalog into it page by page through
the export API while `products` keeps serving the old index, then moves the alias in one atomic
`_aliases` request and prints the new index name. Runs are recorded in `reindex_aux.reindex_runs`.

The production catalog takes hours to backfill and the storefront does not stop writing while it
does; a reindex that needs the copy to be run twice, or the catalog to be paused, cannot be
scheduled. Old indices are kept after a swap (cleanup is manual).

`bin/reindex` runs as a job container on a shared ops node, and ops nodes get drained without
warning (deploys, capacity reclaim). Draining pauses the job's container on the spot, mid-page or
mid-request, and removes it later, sometimes hours later; a paused job never runs another
instruction. Until it is removed, its connections stay up: its sessions on the indexer's database
stay open, with whatever they hold (we have found a drained job's session in `pg_stat_activity`,
idle, hours after the drain). Ops start the same command line on another node as soon as they see
the drain, without waiting for the old container to go. That run has to carry on with the reindex
that was interrupted, not start another: what was already copied is not copied again, and the
storefront's changes made while no reindex process was running are not lost.

## Local development

`python3 -m dev.stack --help`: provisions a small catalog database and search domain on the local
AWS endpoint (`AWS_ENDPOINT_URL`), seeds them, and shows how to run every process and make app
changes by hand.
