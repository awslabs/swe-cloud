"""Plan options: timeout modifiers, inserted statements, schema filters and plan validation."""

from harness import run_program
from planlib import S, T20M, assert_plan, decode_plan, plan, plan_change

OLD = "CREATE TABLE t(id int PRIMARY KEY, a int); CREATE INDEX t_a ON t(a); CREATE TABLE legacy(id int);"
NEW = "CREATE TABLE t(id int PRIMARY KEY, a int, b text); CREATE INDEX t_a ON t(a, id); CREATE TABLE fresh(x int, y bigint);"


def test_timeout_modifiers_last_match_wins(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, OLD, NEW, args=[
        "-t", 'pattern="CREATE INDEX" timeout=2m',
        "--statement-timeout-modifier", 'pattern="INDEX" timeout=7s',
        "-l", 'pattern="^ALTER TABLE" timeout=1500ms',
        "--lock-timeout-modifier", 'pattern="DROP" timeout=1m30s',
    ])
    expected = [
        S('ALTER INDEX "public"."t_a" RENAME TO "pgschemadiff_tmpidx_t_a_{x}"', 7000),
        S('CREATE TABLE "public"."fresh" ("y" bigint, "x" integer)'),
        S('ALTER TABLE "public"."t" ADD COLUMN "b" text COLLATE "pg_catalog"."default"', 3000, 1500),
        S("CREATE INDEX CONCURRENTLY t_a ON public.t USING btree (a, id)", 7000, hazards={"INDEX_BUILD"}),
        S('DROP INDEX CONCURRENTLY "public"."pgschemadiff_tmpidx_t_a_{x}"', 7000, 90000, hazards={"INDEX_DROPPED"}),
        S('DROP TABLE "public"."legacy"', T20M, 90000, hazards={"DELETES_DATA"}),
    ]
    assert_plan(actual, expected, order=[(0, 3, 4)])


def test_inserted_statements_take_their_positions(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, OLD, NEW, args=[
        "-t", 'pattern="INDEX" timeout=7s',
        "-s", 'index=1 statement="SELECT pg_sleep(0)" timeout=5s lock_timeout=2s',
        "--insert-statement", 'index=0 statement="SELECT 1 AS \\"x\\"" timeout=1m lock_timeout=10s',
        "-s", 'index=8 statement="ANALYZE t" timeout=1h lock_timeout=250ms',
    ])
    user = {"IS_USER_GENERATED"}
    expected = [
        S('SELECT 1 AS "x"', 60000, 10000, hazards=user),
        S("SELECT pg_sleep(0)", 5000, 2000, hazards=user),
        S("ANALYZE t", 3600000, 250, hazards=user),
        S('ALTER INDEX "public"."t_a" RENAME TO "pgschemadiff_tmpidx_t_a_{x}"', 7000),
        S('CREATE TABLE "public"."fresh" ("y" bigint, "x" integer)'),
        S('ALTER TABLE "public"."t" ADD COLUMN "b" text COLLATE "pg_catalog"."default"'),
        S("CREATE INDEX CONCURRENTLY t_a ON public.t USING btree (a, id)", 7000, hazards={"INDEX_BUILD"}),
        S('DROP INDEX CONCURRENTLY "public"."pgschemadiff_tmpidx_t_a_{x}"', 7000, hazards={"INDEX_DROPPED"}),
        S('DROP TABLE "public"."legacy"', T20M, hazards={"DELETES_DATA"}),
    ]
    assert_plan(actual, expected, order=[(3, 6, 7)])
    # Inserted statements are positioned exactly; the modifiers do not touch them.
    assert [s["ddl"] for s in (actual[0], actual[2], actual[8])] == [expected[0].ddl, expected[1].ddl, expected[2].ddl]


def test_insert_statement_index_out_of_range_fails(server, db, schema_dir):
    db.execute(OLD)
    directory = schema_dir({"s.sql": NEW})
    result = plan(["--from-dsn", db.dsn, "--to-dir", directory, "-s", 'index=7 statement="SELECT 1" timeout=1s lock_timeout=1s'],
                  server, expect_ok=False)
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_include_and_exclude_schema(server, db, schema_dir):
    db.execute("CREATE SCHEMA other; CREATE TABLE other.o(id int); CREATE TABLE pub(id int);")
    directory = schema_dir({"a.sql": "CREATE SCHEMA other; CREATE TABLE other.o(id int, extra int); CREATE TABLE pub(id int, extra int);"})
    both = [S('ALTER TABLE "other"."o" ADD COLUMN "extra" integer'), S('ALTER TABLE "public"."pub" ADD COLUMN "extra" integer')]
    assert_plan(plan(["--from-dsn", db.dsn, "--to-dir", directory], server), both)
    assert_plan(plan(["--from-dsn", db.dsn, "--to-dir", directory, "--include-schema", "other"], server), both[:1])
    assert_plan(plan(["--from-dsn", db.dsn, "--to-dir", directory, "--exclude-schema", "other"], server), both[1:])
    assert_plan(plan(["--from-dsn", db.dsn, "--to-dir", directory, "--include-schema", "public", "--include-schema", "other"], server), both)


def test_plan_validation_rejects_impossible_plan(server, db, schema_dir):
    db.execute("CREATE TYPE e AS ENUM ('a', 'b', 'c'); CREATE TABLE foo(val e);")
    directory = schema_dir({"a.sql": "CREATE TYPE e AS ENUM ('a', 'c'); CREATE TABLE foo(val e);"})
    refused = plan(["--from-dsn", db.dsn, "--to-dir", directory], server, expect_ok=False)
    assert refused.returncode != 0
    assert refused.stdout.strip() == ""
    unvalidated = plan(["--from-dsn", db.dsn, "--to-dir", directory, "--disable-plan-validation"], server)
    assert_plan(unvalidated, [S('DROP TYPE "public"."e"'), S('''CREATE TYPE "public"."e" AS ENUM ('a', 'c')''')], order=[(0, 1)])


def test_diff_alias_and_flag_spellings(server, db, schema_dir):
    db.execute("CREATE TABLE t(id int);")
    directory = schema_dir({"a.sql": "CREATE TABLE t(id int); CREATE TABLE u(note text, n bigint);"})
    before = server.databases()
    result = run_program(["diff", "--from-dsn", db.dsn, "--to-dir", str(directory), "--output-format=json",
                          "--data-pack-new-tables=false", "--disable-plan-validation"])
    assert result.returncode == 0, result.stderr
    assert server.databases() == before
    assert_plan(decode_plan(result.stdout), [S('CREATE TABLE "public"."u" ("note" text COLLATE "pg_catalog"."default", "n" bigint)')])
