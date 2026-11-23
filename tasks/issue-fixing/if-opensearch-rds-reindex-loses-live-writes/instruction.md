# Brand-facet reindex left search wrong after the swap

We ran the schema-2 reindex (brand facet) on staging with the usual synthetic storefront traffic
running. Search was fine while `bin/reindex` ran. After it swapped the `products` alias to the new
index, QA found:

- products whose price or name was edited during the reindex show the old values in search;
- products that were deleted during the reindex are back in search results;
- some products created during the reindex cannot be found at all.

The old index (still on disk) looked right. We have to run this reindex in production, where the
backfill takes hours and the storefront keeps writing the whole time, so this has to be fixed in
`search-sync` (`/app`; start with `README.md`) before we schedule it.

What we need:

- During a reindex, search through `products` keeps behaving as it does today: every committed
  change shows up within the 2 s merchants expect, and the app's hook calls stay fast.
- When `bin/reindex` finishes, `products` serves the new schema-2 index, and that index matches the
  catalog exactly: every product present with its current data, nothing that was deleted.
- No pausing the storefront, no second full copy of the catalog. The ops node running
  `bin/reindex` gets drained now and then (on staging it happened once mid-backfill, and the rerun
  started over from the first product); ops rerun the same command on another node, and that has
  to finish the interrupted reindex.

The catalog team's code under `/app/catalog` (the write path, the export API, the schema) is not
ours to change, and the indexer's DB role cannot see the catalog tables. Keep the command lines of
`bin/sync-server` and `bin/reindex`, and the `/on_commit` request format. The two processes run on
different hosts. `python3 -m dev.stack --help` (from `/app`) sets up a local stack to try things on.
