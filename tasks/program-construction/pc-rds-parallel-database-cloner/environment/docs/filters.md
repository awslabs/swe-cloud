# parallel-db-cloner — filter files

A filter file is an INI-style text file passed with `--filters <file>`. It
selects which source objects a command handles. The same file gives the same
selection for `clone`, `copy ...`, `list ...` and `compare ...`.

## Syntax

- A section starts with a line `[section-name]`.
- Each following non-empty line is one entry, until the next section.
- Lines starting with `;` or `#` are comments.
- Table and index entries are qualified names `schema.name`; schema entries
  are bare schema names. Names are matched as stored in the catalog
  (case-sensitive, no quoting needed).

```ini
[include-only-table]
sales.orders
sales.customers

[exclude-table-data]
sales.orders

[exclude-index]
sales.orders_placed_at_idx
```

## Sections in scope

| Section | Entries | Meaning |
|---|---|---|
| `include-only-table` | `schema.table` | Handle only the listed tables |
| `exclude-schema` | schema | Skip every object in the listed schemas |
| `exclude-table` | `schema.table` | Skip the listed tables entirely |
| `exclude-table-data` | `schema.table` | Create the table but do not copy its rows |
| `exclude-index` | `schema.index` | Do not create the listed index |

`include-only-table` and `exclude-table` are mutually exclusive: a file
containing both is rejected with a non-zero exit status.

How objects related to a selected or skipped table (its indexes,
constraints, sequences and dependent objects) are treated is part of the
behaviour of each command. `list tables --list-skipped`,
`list sequences --list-skipped` and `list depends --list-skipped` show what a
filter file leaves out.
