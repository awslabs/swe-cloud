# declarative-schema-sync: HCL schema language (PostgreSQL)

Desired states are written in HCL (HashiCorp Configuration Language, native syntax).
A state is a set of top-level blocks; references between blocks use HCL traversals
such as `schema.public`, `column.id` or `table.users.column.id`. Several files may be
passed together (`--to file://a.hcl --to file://b.hcl` or a directory), forming one state.

The same language is produced by `schema inspect`, so an inspected schema can be edited
and applied back.

## `schema`

```hcl
schema "public" {
  comment = "optional comment"
}
```

Each named schema that is managed. With a database-scoped URL the desired state
describes the whole database: list every schema that should exist.

## `enum`

```hcl
enum "status" {
  schema = schema.public
  values = ["active", "inactive"]
}
```

A PostgreSQL enum type. Columns refer to it with `type = enum.status`.

## `table`

```hcl
table "orders" {
  schema  = schema.public
  comment = "optional comment"

  column "id" {
    type = bigserial
  }
  column "user_id" {
    type = bigint
  }
  column "note" {
    type = text
    null = true
  }
  column "total" {
    type    = numeric(10,2)
    default = 0
  }
  column "created_at" {
    type    = timestamptz
    default = sql("now()")
  }

  primary_key {
    columns = [column.id]
  }

  index "orders_user_created" {
    columns = [column.user_id, column.created_at]
  }

  foreign_key "orders_user_fk" {
    columns     = [column.user_id]
    ref_columns = [table.users.column.id]
    on_update   = NO_ACTION
    on_delete   = CASCADE
  }

  check "positive_total" {
    expr = "total >= 0"
  }
}
```

### `column`

| attribute | meaning |
| --- | --- |
| `type` | Column type (see below). Required. |
| `null` | Whether NULL is allowed. Defaults to `false` (NOT NULL). |
| `default` | A literal (`0`, `"text"`, `true`) or a raw SQL expression `sql("expr")`. |
| `comment` | Column comment. |

Types are written as bare identifiers, with arguments in call syntax where the type
takes them, for example `smallint`, `integer`/`int`, `bigint`, `serial`, `bigserial`,
`boolean`, `text`, `varchar(255)`, `char(2)`, `numeric(10,2)`, `decimal(8,2)`, `real`,
`double_precision`, `date`, `time`, `timestamp`, `timestamptz`, `interval`, `uuid`,
`json`, `jsonb`, `bytea`, an enum reference `enum.NAME`, or any type spelled in raw
SQL with `sql("text[]")`. Multi-word SQL type names use underscores. Inspection prints
canonical type names, which may differ from the spelling you wrote.

### `primary_key`

`columns` lists the key columns, in order.

### `index`

| attribute | meaning |
| --- | --- |
| `columns` | Indexed columns, in order. |
| `on { ... }` | Alternative to `columns`: one block per part, with `column = column.X` or `expr = "lower(x)"`, and optional `desc = true`. |
| `unique` | `true` for a unique index. |
| `type` | Index method: `BTREE` (default), `HASH`, `GIN`, `GIST`, `BRIN`, ... |
| `where` | Predicate of a partial index, as SQL text. |

### `foreign_key`

`columns` and `ref_columns` (columns of the referenced table, possibly in another
table block or schema), plus optional `on_update`/`on_delete` with one of
`NO_ACTION`, `RESTRICT`, `CASCADE`, `SET_NULL`, `SET_DEFAULT`.

### `check`

`expr` is the SQL boolean expression of a CHECK constraint.

### `unique`

```hcl
unique "users_email_key" {
  columns = [column.email]
}
```

A UNIQUE table constraint (as opposed to a unique index).

### Column `identity`

```hcl
column "id" {
  type = bigint
  identity {
    generated = BY_DEFAULT   # or ALWAYS
  }
}
```

An identity column.

## Generic HCL notes

* Strings use double quotes; `${...}` interpolation and heredocs follow HCL rules.
* Comments: `#`, `//` and `/* ... */`.
* Attribute order inside a block is not significant; block order within a file is not
  significant either (dependencies are resolved by reference).
