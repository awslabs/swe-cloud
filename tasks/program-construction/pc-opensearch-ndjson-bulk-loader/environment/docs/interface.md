# ndjson-bulk-loader — interface

`ndjson-bulk-loader` is a command-line bulk loader. It reads newline-delimited JSON
(one JSON object per line) from a file or standard input and indexes the documents
into an OpenSearch (Elasticsearch-compatible) index through the HTTP bulk API,
using several parallel workers.

## Invocation

```
program [flags] [FILE]
```

* `FILE` – path of the NDJSON input. If omitted, documents are read from standard input.
* Flags use Go `flag` syntax: `-name value`, `-name=value`, and boolean flags as
  `-name` (or `-name=true|false`). Flags must come before `FILE`.
* Blank lines (also lines with only whitespace) are ignored; other lines are trimmed and sent as documents.
* The exit status is `0` on success and non-zero on failure. Diagnostics are written
  to standard error as log lines prefixed with a `YYYY/MM/DD HH:MM:SS ` timestamp.
  Flag syntax errors are reported the way Go's `flag` package reports them.

### Flags in scope

| Flag | Meaning |
|------|---------|
| `-server URL` | OpenSearch HTTP endpoint, e.g. `http://host:9200`. Repeatable; each request goes to one of the given servers chosen at random. A value without a scheme gets `http://` prepended. Default `http://localhost:9200`. |
| `-index NAME` | Target index (required). The index is created if it does not exist. |
| `-id FIELDS` | Use document fields as the document `_id`. Without `-id`, documents are sent without an id and OpenSearch generates one. A field may be a dotted path into nested objects (`a.b.c`). Several fields may be given separated by commas or spaces; their values are joined into one id. |
| `-optype OP` | Bulk action: `index` (default; replaces existing documents), `create` (only creates new documents), `update` (create or update existing documents). |
| `-mapping M` | Mapping applied to the index before indexing. `M` is either a path to a file containing the mapping JSON or the JSON text itself. |
| `-purge` | Delete any existing index before indexing. |
| `-purge-pause D` | Pause after the purge (Go duration such as `1s`, `500ms`; default `1s`). |
| `-size N` | Number of documents per bulk request (default 1000). |
| `-w N` | Number of parallel workers (default: number of CPUs). |
| `-z` | Input is gzip-compressed. |
| `-r INTERVAL` | Index `refresh_interval` to set when the run finishes (default `1s`). |
| `-0` | Set `number_of_replicas` to 0 while indexing. |
| `-p NAME` | Ingest pipeline used to preprocess documents. |
| `-c CONFIG` | Index creation body (settings/mappings/aliases), a file path or the JSON text, used when the index is created. |
| `-skipbroken` | Skip input lines that are not valid JSON. |
| `-type T` | Legacy document type (only for old Elasticsearch servers; leave empty for OpenSearch). |
| `-timeout D` | HTTP request timeout (default `30s`). |
| `-seed N` | Seed for the random server selection. |
| `-v` | Print the program version and exit. |
| `-h` | Print the flag summary. |

Out of scope: basic auth / API key / TLS options (`-u`, `-apikey`, `-k`), `-verbose`
progress and timing output, and profiling flags (`-cpuprofile`, `-memprofile`).

During a run the loader adjusts index settings for bulk loading and sets them
again when it finishes. Look at the index settings after a run (for example
`refresh_interval` and `number_of_replicas`) to see what it leaves behind.

## OpenSearch endpoint in the AWS venue

The program talks to OpenSearch over plain HTTP. There is no security plugin, so
no authentication is needed. In this environment, an OpenSearch domain is created
through the AWS OpenSearch Service API of the emulator at `$AWS_ENDPOINT_URL`
(region `us-east-1`, dummy credentials are already set):

```python
import boto3, time
c = boto3.client("opensearch")
c.create_domain(DomainName="my-search", EngineVersion="OpenSearch_2.19",
                ClusterConfig={"InstanceType": "m5.large.search", "InstanceCount": 1})
while True:
    st = c.describe_domain(DomainName="my-search")["DomainStatus"]
    if st.get("Endpoint") and not st.get("Processing"):
        break
    time.sleep(2)
endpoint = st["Endpoint"]          # host:port of the data plane
server = endpoint if endpoint.startswith("http") else "http://" + endpoint
# ... program -server "$server" -index docs input.ndjson
c.delete_domain(DomainName="my-search")
```

The domain becomes usable once its `/_cluster/health` reports `green` or
`yellow`. It is a single node, so an index with replicas reports `yellow`.

## Observing results

Index state can be inspected with any HTTP client (`curl`) or with the
`opensearch-py` client that is installed in the environment:

```python
from opensearchpy import OpenSearch
os_ = OpenSearch(server)
os_.indices.refresh(index="docs")
os_.count(index="docs")["count"]
os_.search(index="docs", body={"size": 100})["hits"]["hits"]   # _id and _source
os_.indices.get_mapping(index="docs")
os_.indices.get_settings(index="docs")
```

Refresh the index (or wait for its refresh interval) before you count or search
it. Documents are spread across workers and batches, so the order in which they
reach the index is not defined. Compare documents by `_id` and `_source`, not by
the order they come back in.

## How results are compared

The program is judged by what it leaves in OpenSearch and by what it prints:

* **Documents** – the set of `_id`s and each document's `_source`. Sources are
  compared as JSON values: object key order and whitespace do not matter, numbers
  are compared by numeric value (`1`, `1.0` and `1e0` are equal), and booleans,
  `null`, strings and numbers are never equal to one another. Ids taken from
  fields (`-id`) are compared as exact strings. Ids that OpenSearch generated are
  only checked to be OpenSearch-generated ids.
* **Index state** – whether the index exists, its mapping properties and its
  settings (`refresh_interval`, `number_of_replicas`, `number_of_shards`) after the run.
* **Process results** – exit status, standard output and standard error. On
  standard error the timestamp prefix is removed before comparison. Messages the
  program composes itself are compared as complete lines; where a message embeds
  an OpenSearch response body, only the program's own text and the essential
  part of the server's reason are checked. When input lines are repeated in
  diagnostics they are compared verbatim; the spacing of the bulk action lines the
  program generates itself is not graded.
* Where the outcome or the order of messages depends on how documents are split
  into batches, the graded runs use a single worker (`-w 1`), so the batches are
  consecutive groups of input documents.
