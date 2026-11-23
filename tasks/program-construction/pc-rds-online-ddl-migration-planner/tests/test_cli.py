"""Invocation, schema sources, connection options and output serialization."""

import json
import uuid

import pytest

from harness import run_program
from planlib import S, T20M, assert_plan, decode_plan, parse_sql_plan, plan, plan_change

OLD = "CREATE TABLE t(id int PRIMARY KEY, a int); CREATE INDEX t_a ON t(a); CREATE TABLE legacy(id int);"
NEW = "CREATE TABLE t(id int PRIMARY KEY, a int, b text); CREATE INDEX t_a ON t(a, id); CREATE TABLE fresh(x int, y bigint);"
NEW_PLAN = [
    S('ALTER INDEX "public"."t_a" RENAME TO "pgschemadiff_tmpidx_t_a_{x}"'),
    S('CREATE TABLE "public"."fresh" ("y" bigint, "x" integer)'),
    S('ALTER TABLE "public"."t" ADD COLUMN "b" text COLLATE "pg_catalog"."default"'),
    S("CREATE INDEX CONCURRENTLY t_a ON public.t USING btree (a, id)", T20M, hazards={"INDEX_BUILD"}),
    S('DROP INDEX CONCURRENTLY "public"."pgschemadiff_tmpidx_t_a_{x}"', T20M, hazards={"INDEX_DROPPED"}),
    S('DROP TABLE "public"."legacy"', T20M, hazards={"DELETES_DATA"}),
]


@pytest.mark.parametrize("args", [
    ["plan", "--to-dir", "{dir}"],
    ["plan", "--from-dsn", "{dsn}"],
    ["plan", "--from-dir", "{dir}", "--from-dsn", "{dsn}", "--to-dir", "{dir}"],
    ["plan", "--from-dsn", "{dsn}", "--to-dir", "{dir}", "--to-dsn", "{dsn}"],
    ["plan", "--from-dir", "{dir}", "--to-dir", "{dir}"],
    ["plan", "--from-dsn", "{dsn}", "--to-dir", "{dir}", "--output-format", "yaml"],
    ["plan", "--from-dsn", "{dsn}", "--to-dir", "{dir}", "-t", "timeout=5m"],
    ["plan", "--from-dsn", "{dsn}", "--to-dir", "{dir}", "-t", 'pattern="x" timeout=bogus'],
    ["plan", "--from-dsn", "{dsn}", "--to-dir", "{dir}", "-l", 'pattern="[" timeout=5m'],
    ["plan", "--from-dsn", "{dsn}", "--to-dir", "{dir}", "-t", 'pattern="x" timeout=5m extra=1'],
    ["plan", "--from-dsn", "{dsn}", "--to-dir", "{dir}", "-s", 'index=0 statement="SELECT 1" timeout=1s'],
    ["plan", "--from-dsn", "{dsn}", "--to-dir", "{dir}", "-s", 'index=one statement="SELECT 1" timeout=1s lock_timeout=1s'],
    ["plan", "--from-dsn", "{dsn}", "--to-dir", "{missing}"],
    ["apply", "--from-dsn", "{dsn}", "--skip-confirm-prompt"],
    ["dump"],
    ["no-such-command"],
], ids=["no-from", "no-to", "two-from", "two-to", "no-server", "bad-format", "modifier-no-pattern", "modifier-bad-duration",
        "modifier-bad-regex", "modifier-unknown-key", "insert-no-lock-timeout", "insert-bad-index", "missing-dir", "apply-no-to",
        "dump-no-dsn", "unknown-command"])
def test_invalid_invocations_fail(server, db, schema_dir, args):
    directory = schema_dir({"a.sql": "CREATE TABLE t(id int);"})
    filled = [a.format(dir=directory, dsn=db.dsn, missing=directory / "absent") for a in args]
    before = server.databases()
    result = run_program(filled)
    assert result.returncode != 0, f"expected failure, got stdout {result.stdout[:500]!r}"
    assert '"statements"' not in result.stdout
    assert server.databases() == before
    assert db.query("select relname from pg_class where relnamespace = 'public'::regnamespace") == []


def test_json_plan_document(server, db, schema_dir):
    db.execute(OLD)
    directory = schema_dir({"s.sql": NEW})
    result = run_program(["plan", "--from-dsn", db.dsn, "--to-dir", directory, "--output-format", "json"])
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    for entry in document["statements"]:
        assert not entry["ddl"].rstrip().endswith(";")
        for hazard in entry["hazards"] or []:
            assert isinstance(hazard.get("message", ""), str)
    assert_plan(decode_plan(result.stdout), NEW_PLAN, order=[(0, 3, 4)])


def test_sql_output_format(server, db, schema_dir):
    db.execute(OLD)
    directory = schema_dir({"s.sql": NEW})
    for extra in ([], ["--output-format", "sql"]):
        result = run_program(["plan", "--from-dsn", db.dsn, "--to-dir", directory, *extra])
        assert result.returncode == 0, result.stderr
        numbers, statements = parse_sql_plan(result.stdout)
        assert numbers == list(range(len(NEW_PLAN)))
        assert_plan(statements, NEW_PLAN, order=[(0, 3, 4)])


def test_empty_plan_in_each_format(server, db, schema_dir):
    ddl = "CREATE TABLE t(id int PRIMARY KEY, a int CHECK (a > 0)); CREATE INDEX t_a ON t(a);"
    db.execute(ddl)
    directory = schema_dir({"s.sql": ddl})
    sql = run_program(["plan", "--from-dsn", db.dsn, "--to-dir", directory])
    assert sql.returncode == 0 and sql.stdout.strip() == ""
    js = run_program(["plan", "--from-dsn", db.dsn, "--to-dir", directory, "--output-format", "json"])
    assert js.returncode == 0
    assert json.loads(js.stdout)["statements"] in (None, [])
    assert decode_plan(js.stdout) == []


def test_directory_sources_are_recursive_and_filtered(server, db, schema_dir):
    directory = schema_dir({
        "a/1.SQL": "CREATE TABLE t1(id int PRIMARY KEY);",
        "b/2.sql": "CREATE TABLE t2(id int REFERENCES t1(id));",
        "c/deeper/z.Sql": "CREATE INDEX t2_idx ON t2(id);\n-- trailing comment\n",
        "notes.txt": "this is not SQL at all",
        "d/readme.sql.bak": "DROP TABLE nonsense;",
    })
    actual = plan(["--from-dsn", db.dsn, "--to-dir", directory], server)
    expected = [
        S('CREATE TABLE "public"."t1" ("id" integer NOT NULL)'),
        S("CREATE UNIQUE INDEX CONCURRENTLY t1_pkey ON public.t1 USING btree (id)", T20M),
        S('ALTER TABLE "public"."t1" ADD CONSTRAINT "t1_pkey" PRIMARY KEY USING INDEX "t1_pkey"'),
        S('CREATE TABLE "public"."t2" ("id" integer)'),
        S('ALTER TABLE "public"."t2" ADD CONSTRAINT "t2_id_fkey" FOREIGN KEY (id) REFERENCES t1(id) NOT VALID'),
        S('ALTER TABLE "public"."t2" VALIDATE CONSTRAINT "t2_id_fkey"'),
        S("CREATE INDEX CONCURRENTLY t2_idx ON public.t2 USING btree (id)", T20M),
    ]
    assert_plan(actual, expected, order=[(0, 1, 2, 4, 5), (3, 4), (3, 6)])


def test_directory_files_run_in_lexical_order(server, db, schema_dir):
    ordered = schema_dir({"10_types/mood.sql": "CREATE TYPE mood AS ENUM ('x');", "20_tables.sql": "CREATE TABLE t2(id int, m mood);"})
    actual = plan(["--from-dsn", db.dsn, "--to-dir", ordered], server)
    assert_plan(actual, [S('''CREATE TYPE "public"."mood" AS ENUM ('x')'''), S('CREATE TABLE "public"."t2" ("id" integer, "m" mood)')],
                order=[(0, 1)])
    reversed_ = schema_dir({"a.sql": "CREATE TABLE t2(id int, m mood);", "b.sql": "CREATE TYPE mood AS ENUM ('x');"})
    result = plan(["--from-dsn", db.dsn, "--to-dir", reversed_], server, expect_ok=False)
    assert result.returncode != 0
    assert '"statements"' not in result.stdout


def test_invalid_sql_in_directory_fails_cleanly(server, db, schema_dir):
    directory = schema_dir({"0.sql": "CREATE TABLE ok(id int);", "1.sql": "CREATE TABLE broken(id int;"})
    result = plan(["--from-dsn", db.dsn, "--to-dir", directory], server, expect_ok=False)
    assert result.returncode != 0
    assert '"statements"' not in result.stdout


def test_multiple_to_dirs_are_combined(server, db, schema_dir):
    first = schema_dir({"0.sql": "CREATE TYPE color AS ENUM ('red', 'green');"})
    second = schema_dir({"0.sql": "CREATE TABLE paint(c color);"})
    actual = plan(["--from-dsn", db.dsn, "--to-dir", first, "--to-dir", second], server)
    assert_plan(actual, [S('''CREATE TYPE "public"."color" AS ENUM ('red', 'green')'''), S('CREATE TABLE "public"."paint" ("c" color)')],
                order=[(0, 1)])


def test_dir_and_dsn_combinations(server, make_db, schema_dir):
    temp = make_db()
    target = make_db("CREATE TABLE z(id int, q text);")
    v1 = schema_dir({"a.sql": "CREATE TABLE z(id int);"})
    v2 = schema_dir({"a.sql": "CREATE TABLE z(id int, q text);"})
    add_q = [S('ALTER TABLE "public"."z" ADD COLUMN "q" text COLLATE "pg_catalog"."default"')]
    assert_plan(plan(["--from-dir", v1, "--to-dir", v2, "--temp-db-dsn", temp.dsn], server), add_q)
    assert_plan(plan(["--from-dir", v1, "--to-dsn", target.dsn], server), add_q)
    assert_plan(plan(["--from-dsn", target.dsn, "--to-dir", v1], server), [S('ALTER TABLE "public"."z" DROP COLUMN "q"', hazards={"DELETES_DATA"})])
    assert plan(["--from-dir", v2, "--to-dsn", target.dsn], server) == []
    # Neither source touched the databases it read.
    assert temp.query("select relname from pg_class where relnamespace = 'public'::regnamespace") == []


def test_connection_from_environment(server, db, schema_dir):
    directory = schema_dir({"a.sql": "CREATE TABLE z(id int);"})
    expected = [S('CREATE TABLE "public"."z" ("id" integer)')]
    pg_env = {"PGHOST": server.host, "PGPORT": str(server.port), "PGUSER": "app", "PGPASSWORD": "verify-pass-7", "PGDATABASE": db.name}
    result = run_program(["plan", "--from-empty-dsn", "--to-dir", directory, "--output-format", "json"], env=pg_env)
    assert result.returncode == 0, result.stderr
    assert_plan(decode_plan(result.stdout), expected)
    no_password = server.dsn(db.name, password=None)
    result = run_program(["plan", "--from-dsn", no_password, "--to-dir", directory, "--output-format", "json"],
                         env={"PGPASSWORD": "verify-pass-7"})
    assert result.returncode == 0, result.stderr
    assert_plan(decode_plan(result.stdout), expected)
    wrong = run_program(["plan", "--from-dsn", server.dsn(db.name, password="wrong-pass"), "--to-dir", directory, "--output-format", "json"])
    assert wrong.returncode != 0
    assert '"statements"' not in wrong.stdout


def test_temp_database_server_selection(server, db, schema_dir):
    role = "limited_" + uuid.uuid4().hex[:8]
    db.execute("CREATE TABLE t(id int);")
    db.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'limited-pass-3' NOCREATEDB")
    try:
        directory = schema_dir({"a.sql": "CREATE TABLE t(id int, v text);"})
        limited = server.dsn(db.name, user=role, password="limited-pass-3")
        refused = plan(["--from-dsn", limited, "--to-dir", directory], server, expect_ok=False)
        assert refused.returncode != 0
        assert '"statements"' not in refused.stdout
        actual = plan(["--from-dsn", limited, "--to-dir", directory, "--temp-db-dsn", server.dsn("postgres")], server)
        assert_plan(actual, [S('ALTER TABLE "public"."t" ADD COLUMN "v" text COLLATE "pg_catalog"."default"')])
    finally:
        with server.connect("postgres") as conn:
            conn.execute(f"DROP ROLE IF EXISTS {role}")
