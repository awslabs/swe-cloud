# table-sync-runner: replication files

A replication file is a YAML (or JSON) document that `program run -r FILE` executes.
It names one source connection, one target connection, defaults, and a map of
streams. Streams run one after another, and each stream reports its own success
or failure.

```yaml
source: SRC                 # connection name (see interface.md)
target: LAKE

defaults:                   # applied to every stream unless the stream overrides it
  mode: full-refresh
  object: 'analytics.{stream_schema}_{stream_table}'
  target_options:
    column_casing: source

streams:
  sales.orders:             # schema.table in the source
    mode: incremental
    primary_key: [id]
    update_key: updated_at

  sales.customers:          # empty value: defaults only

  sales.*:                  # wildcard: every table in schema "sales"

  recent_orders:            # custom stream name with a SQL query
    sql: select id, customer, amount from sales.orders where amount > 0
    object: 'analytics.recent_orders'
    mode: snapshot
```

## Stream keys

| Key | Meaning |
|---|---|
| `object` | Target object. For database and Iceberg targets: `schema.table` (Iceberg: Glue database `.` table). For S3: a key or prefix inside the bucket, optionally with a file extension. May contain template variables. |
| `mode` | `full-refresh`, `truncate`, `incremental` or `snapshot`. Default: `full-refresh`. |
| `primary_key` | Column name or list of column names identifying a row. |
| `update_key` | Column whose increasing values mark new or changed rows (incremental mode). |
| `sql` | Query used instead of reading the table named by the stream key. Inside an incremental stream's query, `{incremental_where_cond}` stands for the incremental filter. |
| `select` | List of columns to keep. A leading `-` excludes a column instead. |
| `where` | Filter condition applied when reading a table. |
| `columns` | Mapping of column name to the type it is cast to (e.g. `amount: decimal(12,2)`, `id: bigint`). |
| `disabled` | `true` skips the stream. |
| `source_options` | Mapping of source options, such as `limit`. |
| `target_options` | Mapping of target options (see below). |

A stream key may be `schema.table`, or a wildcard such as `schema.*` or
`schema.prefix_*`, which expands to the matching source tables. A key that is not
a source table names a custom stream and needs `sql`. Settings given on a stream
override those in `defaults`. Nested mappings such as `target_options` merge key
by key.

## Template variables

These variables can appear in `object` (and in `sql`) between braces:

| Variable | Value |
|---|---|
| `{stream_schema}`, `{stream_table}` | schema and table of the source stream |
| `{stream_schema_lower}`, `{stream_schema_upper}`, `{stream_table_lower}`, `{stream_table_upper}` | case-converted forms |
| `{stream_name}` | the stream key (lower case) |

## Target options

| Option | Meaning |
|---|---|
| `column_casing` | How target column names are spelled: `source`, `target`, `snake`, `upper`, `lower` or `normalize`. |
| `add_new_columns` | Whether columns that appear in the source are added to an existing target. |
| `format` | File targets: `csv` or `jsonlines`. Default: derived from the object's extension. |
| `file_max_rows` | File targets: maximum rows per output file. |
| `header` | CSV: whether to write a header row. |
| `delimiter` | CSV: field delimiter. |
| `compression` | File targets: e.g. `gzip`. Default: no compression. |
| `table_keys` | Database targets: e.g. `primary: [id]` to declare keys on a created table. |

How each mode treats existing target data, how incremental progress is
determined, how values are typed in each target, how files are named and split,
and what happens when the source and target shapes differ are properties of the
program. Discover them by running it.
