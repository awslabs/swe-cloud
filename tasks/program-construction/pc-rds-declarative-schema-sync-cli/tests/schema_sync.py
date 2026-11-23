"""Verifier helpers: per-test databases on the shared RDS instance, program runs and observations.

Expected values in the test modules were observed from the reference program (see curation notes);
these helpers only parse program output and read the PostgreSQL catalog.
"""

import json
import re
import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import boto3
import hcl2
import psycopg

from harness import give_to_candidate, run_program, write_fixture

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = Path(__file__).parent / "expected"
QUIET = {"ATLAS_NO_UPDATE_NOTIFIER": "1", "ATLAS_NO_UPGRADE_SUGGESTIONS": "1"}
SYSTEM_SCHEMAS = "('pg_catalog', 'information_schema', 'pg_toast')"


class Server:
    """The RDS PostgreSQL instance shared by the session."""

    def __init__(self, host, port, user, password):
        self.host, self.port, self.user, self.password = host, port, user, password

    def url(self, db, schema=None, password=None):
        secret = quote(self.password if password is None else password, safe="")
        url = f"postgres://{self.user}:{secret}@{self.host}:{self.port}/{db}?sslmode=disable"
        return url + (f"&search_path={schema}" if schema else "")

    def connect(self, db):
        return psycopg.connect(host=self.host, port=self.port, user=self.user, password=self.password,
                               dbname=db, autocommit=True, connect_timeout=10)

    def execute(self, db, sql, params=None):
        with self.connect(db) as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall() if cursor.description else []


class Context:
    """One test's workspace, databases and AWS parameters, all removed at teardown."""

    def __init__(self, server, root):
        self.server = server
        self.root = root
        self.home = root / "home"
        self.home.mkdir(mode=0o755)
        give_to_candidate(self.home)
        self.databases = []
        self.roles = []
        self.secrets = []
        self.parameters = []

    def database(self, owner=None):
        name = "t_" + uuid.uuid4().hex[:12]
        self.server.execute("postgres", f'CREATE DATABASE "{name}"' + (f' OWNER "{owner}"' if owner else ""))
        self.databases.append(name)
        return name

    def role(self, password):
        name = "u_" + uuid.uuid4().hex[:10]
        self.server.execute("postgres", f'CREATE ROLE "{name}" LOGIN PASSWORD ' + "'" + password.replace("'", "''") + "'")
        self.server.execute("postgres", f'GRANT "{name}" TO "{self.server.user}"')
        self.roles.append(name)
        return name, password

    def url(self, db, schema=None, password=None):
        return self.server.url(db, schema, password)

    def sql(self, db, statement, params=None):
        return self.server.execute(db, statement, params)

    def write(self, name, text):
        path = self.root / name
        write_fixture(path, text)
        return path

    def fixture(self, name, target=None):
        return self.write(target or name, (FIXTURES / name).read_text())

    def run(self, *args, cwd=None, env=None):
        return run_program(list(args), cwd=cwd or self.root, env={**QUIET, "HOME": str(self.home), **(env or {})},
                           timeout=40)

    def secret(self, value):
        name = f"sync/{uuid.uuid4().hex[:10]}/db"
        boto3.client("secretsmanager").create_secret(Name=name, SecretString=value)
        self.secrets.append(name)
        return name

    def parameter(self, value, kind="SecureString", prefix="/"):
        name = f"{prefix}sync/{uuid.uuid4().hex[:10]}/value"
        boto3.client("ssm").put_parameter(Name=name, Value=value, Type=kind)
        self.parameters.append(name)
        return name

    def catalog(self, db):
        return catalog(self.server, db)

    def close(self):
        errors = []
        for name in self.databases:
            try:
                self.server.execute("postgres", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except psycopg.Error as error:
                errors.append(error)
        for name in self.roles:
            try:
                self.server.execute("postgres", f'DROP ROLE IF EXISTS "{name}"')
            except psycopg.Error as error:
                errors.append(error)
        for name in self.secrets:
            try:
                boto3.client("secretsmanager").delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
            except Exception as error:  # noqa: BLE001 -- best-effort cleanup of owned resources
                errors.append(error)
        for name in self.parameters:
            try:
                boto3.client("ssm").delete_parameter(Name=name)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        if errors:
            print("cleanup errors:", errors)


def expected(name):
    """A native-observed output kept under expected/."""
    return (EXPECTED / name).read_text()


# ---------------------------------------------------------------- results


def ok(result):
    assert result.returncode == 0, f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result.stdout


def fails(result):
    """A failed invocation: exit status 1, a diagnostic on stderr and no plan on stdout."""
    assert result.returncode == 1, f"expected exit 1, got {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.stderr.strip(), "expected a diagnostic on stderr"
    return result.stderr


def errors(result):
    """A failure reported as `Error: ...` on stderr."""
    stderr = fails(result)
    assert stderr.lstrip().startswith("Error: "), stderr
    return stderr


# ---------------------------------------------------------------- plans


def squash(sql):
    """Whitespace-only normalization: runs collapse to one space; none next to parentheses or commas."""
    sql = re.sub(r"\s+", " ", sql.strip())
    return re.sub(r" ?([(),]) ?", r"\1", sql)


def statements(output):
    """SQL statements of a plan, without the `-- ` comment lines."""
    kept = "\n".join(line for line in output.splitlines() if not line.lstrip().startswith("--")).strip()
    if not kept:
        return []
    return [squash(part) for part in re.split(r";[ \t]*(?:\n|\Z)", kept) if part.strip()]


def assert_plan(output, expected, after=()):
    """Statements equal `expected` as a multiset; each (i, j) in `after` requires expected[i] before expected[j]."""
    got = statements(output)
    want = [squash(statement) for statement in expected]
    missing = list((Counter(want) - Counter(got)).elements())
    extra = list((Counter(got) - Counter(want)).elements())
    assert not missing and not extra, f"plan differs\nmissing: {missing}\nunexpected: {extra}\noutput:\n{output}"
    for first, second in after:
        assert got.index(want[first]) < got.index(want[second]), (
            f"{expected[first]!r} must precede {expected[second]!r}\noutput:\n{output}")


def assert_apply_plan(output, expected, after=()):
    lines = output.splitlines()
    assert lines and lines[0].strip() == "-- Planned Changes:", output
    assert_plan(output, expected, after)


def assert_synced(output):
    assert ";" not in output and "synced" in output.lower(), output


# ---------------------------------------------------------------- catalog

CATALOG_QUERIES = [
    f"""SELECT 'col', table_schema, table_name, column_name, data_type, udt_name, character_maximum_length,
               numeric_precision, numeric_scale, is_nullable, column_default
        FROM information_schema.columns WHERE table_schema NOT IN {SYSTEM_SCHEMAS}
        ORDER BY table_schema, table_name, ordinal_position""",
    f"""SELECT 'con', n.nspname, c.relname, k.conname, k.contype, pg_get_constraintdef(k.oid)
        FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN {SYSTEM_SCHEMAS}""",
    f"""SELECT 'idx', schemaname, indexname, indexdef FROM pg_indexes WHERE schemaname NOT IN {SYSTEM_SCHEMAS}""",
    """SELECT 'enum', n.nspname, t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
       FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid JOIN pg_namespace n ON n.oid = t.typnamespace
       GROUP BY n.nspname, t.typname""",
    """SELECT 'schema', nspname, obj_description(oid, 'pg_namespace') FROM pg_namespace
       WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'""",
    f"""SELECT 'cmt', n.nspname, c.relname, coalesce(a.attname, ''), d.description
        FROM pg_description d JOIN pg_class c ON d.classoid = 'pg_class'::regclass AND d.objoid = c.oid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid AND d.objsubid > 0
        WHERE n.nspname NOT IN {SYSTEM_SCHEMAS}""",
]


def canonical(lines):
    """Columns keep ordinal order (it is observable); other catalog rows are compared as sorted sets."""
    columns = [line for line in lines if line.startswith("col|")]
    others = sorted(line for line in lines if not line.startswith("col|"))
    return columns + others


def catalog(server, db):
    lines = []
    with server.connect(db) as conn:
        for query in CATALOG_QUERIES:
            lines += ["|".join(map(str, row)) for row in conn.execute(query).fetchall()]
    return canonical(lines)


def expected_catalog(text):
    return canonical([line.strip() for line in text.strip().splitlines() if line.strip()])


def assert_catalog(ctx, db, text):
    got = ctx.catalog(db)
    want = expected_catalog(text)
    assert got == want, "catalog differs\n" + "\n".join(
        [f"- {line}" for line in want if line not in got] + [f"+ {line}" for line in got if line not in want])


EMPTY_DATABASE = "schema|public|standard public schema"


# ---------------------------------------------------------------- structured documents


NUMBER_EXPRESSION = re.compile(r"^\$\{\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*\}$")


def tagged(value):
    """Type-preserving comparison form: booleans stay distinct from numbers; numbers compare by value."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int | float):
        return ("num", Decimal(str(value)).normalize())
    if isinstance(value, str):
        # python-hcl2 returns some number literals (e.g. `1e0`, `15e-1`) as `${...}` expressions.
        literal = NUMBER_EXPRESSION.match(value)
        if literal:
            return ("num", Decimal(literal.group(1)).normalize())
        return ("str", value)
    if value is None:
        return ("null",)
    if isinstance(value, dict):
        return ("map", tuple(sorted((key, tagged(item)) for key, item in value.items())))
    if isinstance(value, list | tuple):
        return ("list", tuple(tagged(item) for item in value))
    raise TypeError(type(value))


def named(blocks, where):
    """HCL labeled blocks parse to a list of {label: body}; key them by label, rejecting duplicates."""
    result = {}
    for block in blocks:
        for label, body in block.items():
            assert label not in result, f"duplicate {where} block {label!r}"
            result[label] = body
    return result


TABLE_NAMED = ("index", "foreign_key", "check", "unique")


def hcl_document(text):
    """Structure of an HCL state: block and attribute order ignored, except column and index-part order."""
    raw = hcl2.loads(text)
    doc = {}
    for kind, blocks in raw.items():
        entries = named(blocks, kind) if isinstance(blocks, list) and kind in ("table", "enum", "schema") else blocks
        if kind == "table":
            entries = {name: _table(body) for name, body in entries.items()}
        doc[kind] = entries
    return tagged(doc)


def _table(body):
    table = dict(body)
    if "column" in table:
        table["column"] = [(label, column) for block in table["column"] for label, column in block.items()]
    for kind in TABLE_NAMED:
        if kind in table:
            table[kind] = named(table[kind], kind)
    return table


def assert_hcl(actual, expected):
    got, want = hcl_document(actual), hcl_document(expected)
    assert got == want, f"inspected HCL differs\nexpected:\n{expected}\nactual:\n{actual}"


def json_document(text):
    """Inspect JSON: schemas and tables keyed by name; other arrays keep their order."""
    doc = json.loads(text)
    assert isinstance(doc, dict) and set(doc) == {"schemas"}, text
    schemas = {}
    for entry in doc["schemas"]:
        entry = dict(entry)
        if "tables" in entry:
            names = [table["name"] for table in entry["tables"]]
            assert len(names) == len(set(names)), f"duplicate table in {text}"
            entry["tables"] = {table["name"]: table for table in entry["tables"]}
        assert entry["name"] not in schemas, f"duplicate schema in {text}"
        schemas[entry["name"]] = entry
    return tagged(schemas)
