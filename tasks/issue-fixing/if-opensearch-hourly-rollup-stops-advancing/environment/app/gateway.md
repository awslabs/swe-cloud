# OpenSearch access gateway

Production supplies OS_ENDPOINT through a metered gateway. Use this URL for all data-plane calls.
Development endpoints created by admin.py can be accessed directly. The gateway permits:

- GET source/_stats, including level=shards; GET metadata/_doc/<transform-id>;
  GET target/_doc/<id>.
- POST a single configured index's _search or _count. Searches force track_total_hits=true;
  all matched documents are charged on each request, regardless of page size or hits returned.
- PUT/POST target/_doc/<id> and metadata/_doc/<transform-id>; POST _bulk with index/create/update
  actions confined to those documents; POST target/_refresh and metadata/_refresh.

No source GET-by-id, source writes, scroll, PIT, msearch, mget, templates, plugins, index creation,
settings changes, pipelines or other endpoints are available. No additional persistence service
is part of the worker's deployment. An abandoned process may retain queued requests after its replacement starts.

For searches/counts, supported top-level JSON keys are query, size, from, aggs, aggregations, sort,
search_after, _source, track_total_hits, seq_no_primary_term, version, stored_fields,
docvalue_fields. Query parameters are preference, routing, typed_keys, request_cache.
Query clauses: bool, range, term, terms (inline arrays), exists, ids, match_all, match_none,
constant_score. Aggregations: composite (terms/date_histogram/histogram sources), terms,
date_histogram, histogram, range, date_range, filter, filters, sum, value_count, avg, min, max,
stats. Nested aggregations are supported. Scripts are unavailable anywhere. Unsupported shapes
are rejected before forwarding. This includes post_filter, global aggregation, filter_path,
terminate_after, min_score and search_pipeline, which would make hit counts unsuitable for billing.
A response with partial shards or inexact hits.total is a failed search, not committed work.


Target admission: 8 document items per invocation. A rejected individual target write returns
HTTP 429 with error type `es_rejected_execution_exception`. In a bulk request, each admitted
item is forwarded to OpenSearch, and excess items have status 429 and that error type in an
HTTP 200 bulk response with `errors: true`. An item failure never rolls back other items. The
allocation counts items, including updates and identical overwrites, across all requests in
that invocation. Metadata items are exempt. Waiting or issuing another request does not reset
it; the scheduler grants a new allocation to the next process. Exit 75 requests that next process.
Other transport failures can leave the outcome of a sent target item unknown; stable ids still
apply. Do not treat an HTTP-successful bulk response as proof that all its items succeeded.


Document concurrency APIs are available with their native OpenSearch 2.11 semantics. Metadata
GET responses include _seq_no and _primary_term. Metadata PUT/POST and bulk index/update actions
may use if_seq_no and if_primary_term; a stale condition returns HTTP/item status 409 and changes
nothing. A GET followed by an unconditional write is not atomic. Target PUT/POST and bulk index
actions may use version and version_type (external or external_gte). External versions are
positive signed 64-bit integers: external requires a strictly greater value than the stored
version; external_gte also accepts equality. A rejected older version has status 409 and does not
change the document. Bulk actions carry these parameters in the action metadata. Individual
writes carry them in the URL query. Target GET responses include the stored _version. All
admitted target items, including version conflicts, spend credit. No gateway lease, automatic
request cancellation, ownership check or transaction across target and metadata is provided.
