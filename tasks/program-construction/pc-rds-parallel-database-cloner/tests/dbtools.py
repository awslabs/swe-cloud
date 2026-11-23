"""Task-local helpers: database setup/inspection through psycopg and program invocation."""

import re
from dataclasses import dataclass
from urllib.parse import quote

import psycopg
from psycopg import sql

from harness import run_program, write_fixture

USER_SCHEMAS = "n.nspname !~ '^pg_' and n.nspname <> 'information_schema'"


@dataclass(frozen=True)
class Instance:
    host: str
    port: int
    user: str
    password: str

    def uri(self, dbname):
        return f"postgres://{quote(self.user)}:{quote(self.password)}@{self.host}:{self.port}/{dbname}"

    def keyvalue(self, dbname):
        return f"host={self.host} port={self.port} user={self.user} password={self.password} dbname={dbname}"


def drop_database(instance, name):
    try:
        with psycopg.connect(instance.uri("postgres"), autocommit=True) as conn:
            conn.execute(f"drop database if exists {name} with (force)")
    except psycopg.Error:
        pass


def execute(uri, statements):
    """Run a SQL script (several statements) in one transaction."""
    with psycopg.connect(uri) as conn:
        conn.execute(statements)


def query(uri, statement, params=None):
    with psycopg.connect(uri, autocommit=True) as conn:
        return conn.execute(statement, params).fetchall()


def scalar(uri, statement, params=None):
    return query(uri, statement, params)[0][0]


def cloner(args, work, *, env=None, timeout=50):
    """Run /app/program with a private TMPDIR inside the test's workspace."""
    return run_program([str(a) for a in args], cwd=work, env={"TMPDIR": str(work), **(env or {})}, timeout=timeout)


def check(result):
    assert result.returncode == 0, f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr[-4000:]}"
    return result


def write_filters(work, name, text):
    path = work / name
    write_fixture(path, text, mode=0o644)
    return path


# ---------------------------------------------------------------- inventories


def relations(uri):
    """{(schema, name, relkind)} of tables, partitioned tables, views, matviews and sequences."""
    rows = query(
        uri,
        f"""select n.nspname, c.relname, c.relkind::text
              from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where {USER_SCHEMAS} and c.relkind in ('r', 'p', 'v', 'm', 'S')""",
    )
    return set(rows)


def schemas(uri):
    rows = query(uri, f"select n.nspname from pg_namespace n where {USER_SCHEMAS} and n.nspname <> 'public'")
    return {r[0] for r in rows}


def indexes(uri):
    """{(schema, table, index, definition)}."""
    rows = query(
        uri,
        f"""select n.nspname, t.relname, i.relname, pg_get_indexdef(i.oid)
              from pg_index x
              join pg_class i on i.oid = x.indexrelid
              join pg_class t on t.oid = x.indrelid
              join pg_namespace n on n.oid = t.relnamespace
             where {USER_SCHEMAS}""",
    )
    return set(rows)


def index_names(uri):
    return {(schema, name) for schema, _, name, _ in indexes(uri)}


def constraints(uri):
    """{(schema, table, name, type, definition)} of table constraints."""
    rows = query(
        uri,
        f"""select n.nspname, t.relname, c.conname, c.contype::text, pg_get_constraintdef(c.oid)
              from pg_constraint c
              join pg_class t on t.oid = c.conrelid
              join pg_namespace n on n.oid = t.relnamespace
             where {USER_SCHEMAS}""",
    )
    return set(rows)


def constraint_names(uri):
    return {(schema, table, name) for schema, table, name, _, _ in constraints(uri)}


def sequences(uri):
    """{(schema, name): (last_value, is_called)} read from each sequence."""
    result = {}
    with psycopg.connect(uri, autocommit=True) as conn:
        names = conn.execute(
            f"""select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
                 where {USER_SCHEMAS} and c.relkind = 'S'"""
        ).fetchall()
        for schema, name in names:
            row = conn.execute(sql.SQL("select last_value, is_called from {}.{}").format(sql.Identifier(schema), sql.Identifier(name))).fetchone()
            result[(schema, name)] = (row[0], row[1])
    return result


def digest(uri, schema, table, *, only=True):
    """(row count, md5 over the sorted text of every row): equal digests mean equal row multisets."""
    statement = sql.SQL(
        "select count(*), md5(coalesce(string_agg(t::text, E'\\n' order by t::text), '')) from {only} {}.{} t"
    ).format(sql.Identifier(schema), sql.Identifier(table), only=sql.SQL("only" if only else ""))
    with psycopg.connect(uri, autocommit=True) as conn:
        count, checksum = conn.execute(statement).fetchone()
    return count, checksum


def row_count(uri, schema, table):
    return digest(uri, schema, table)[0]


def table_digests(uri):
    """{(schema, table): digest} for every ordinary table (rows of the table itself, not children)."""
    tables = query(
        uri,
        f"""select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where {USER_SCHEMAS} and c.relkind = 'r'""",
    )
    return {(schema, name): digest(uri, schema, name) for schema, name in tables}


def exists(uri, qualified):
    return scalar(uri, "select to_regclass(%s) is not null", (qualified,))


# ------------------------------------------------------------- text tables


class TextTable(list):
    """Rows as {header: cell} dicts; `header` keeps the column names even without rows."""

    def __init__(self, header, rows):
        super().__init__(rows)
        self.header = header

    def column(self, name):
        """The exact header spelling of a column, matched case-insensitively."""
        for key in self.header:
            if key.lower() == name.lower():
                return key
        raise AssertionError(f"column {name!r} missing from header {self.header}")

    def values(self, *names):
        """A list (multiplicity kept) of tuples of the named cells, identifiers unquoted."""
        keys = [self.column(name) for name in names]
        return sorted(tuple(unquote(row[key]) for key in keys) for row in self)


def parse_table(text):
    """Parse the first `a | b` text table in stdout."""
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        if "|" in lines[i] and re.fullmatch(r"[\s\-+|]+", lines[i + 1] or "x") and "-" in lines[i + 1]:
            header = [cell.strip() for cell in lines[i].split("|")]
            rows = []
            for line in lines[i + 2 :]:
                if not line.strip():
                    break
                cells = [cell.strip() for cell in line.split("|")]
                assert len(cells) == len(header), f"ragged table row {line!r} for header {header}"
                rows.append(dict(zip(header, cells)))
            return TextTable(header, rows)
    raise AssertionError(f"no text table found in output:\n{text}")


def unquote(name):
    """Identifiers may be printed quoted; compare their catalog spelling."""
    if len(name) >= 2 and name[0] == name[-1] == '"':
        return name[1:-1].replace('""', '"')
    return name
