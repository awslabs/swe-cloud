"""Multi-step sequences: schema evolution, recovery after failures, dump round trips."""

from harness import run_program
from planlib import (ALL_HAZARDS, S, T20M, apply, assert_plan, assert_same_catalog, catalog, column_order, last_line, plan,
                     rows, split_dump)


def test_schema_evolution_across_three_versions(server, db, schema_dir, oracle):
    v1 = "CREATE TABLE account(id int PRIMARY KEY, email text NOT NULL, created_at timestamptz DEFAULT now());"
    v2 = """CREATE TYPE plan_kind AS ENUM ('free', 'pro');
            CREATE TABLE account(id int PRIMARY KEY, email text NOT NULL, created_at timestamptz DEFAULT now(), kind plan_kind DEFAULT 'free');
            CREATE UNIQUE INDEX account_email ON account(email);
            CREATE TABLE invoice(id bigint PRIMARY KEY, account_id int NOT NULL REFERENCES account(id), cents int CHECK (cents >= 0));"""
    v3 = """CREATE TYPE plan_kind AS ENUM ('free', 'team', 'pro');
            CREATE TABLE account(id int PRIMARY KEY, email text NOT NULL, kind plan_kind NOT NULL DEFAULT 'free');
            CREATE UNIQUE INDEX account_email ON account(lower(email));
            CREATE TABLE invoice(id bigint PRIMARY KEY, account_id int NOT NULL REFERENCES account(id) ON DELETE CASCADE, cents bigint CHECK (cents >= 0));
            CREATE INDEX invoice_account ON invoice(account_id);"""
    dirs = [schema_dir({"schema.sql": v}) for v in (v1, v2, v3)]

    result = apply(server, db, ["--to-dir", dirs[0]], "--allow-hazards", "INDEX_BUILD")
    assert result.returncode == 0, result.stderr[-2000:]
    assert_same_catalog(catalog(db), oracle(v1))
    assert column_order(db, "account") == ["created_at", "id", "email"]
    db.execute("INSERT INTO account(id, email) VALUES (1, 'A@example.com'), (2, 'b@example.com')")

    result = apply(server, db, ["--to-dir", dirs[1]], "--allow-hazards", "INDEX_BUILD")
    assert result.returncode == 0, result.stderr[-2000:]
    assert_same_catalog(catalog(db), oracle(v2))
    assert column_order(db, "invoice") == ["id", "account_id", "cents"]
    db.execute("INSERT INTO invoice VALUES (10, 1, 500), (11, 2, 0)")
    assert db.query("select id, kind::text from account order by id") == [(1, "free"), (2, "free")]

    actual = plan(["--from-dsn", db.dsn, "--to-dir", dirs[2]], server)
    lock, perf = {"ACQUIRES_ACCESS_EXCLUSIVE_LOCK"}, {"IMPACTS_DATABASE_PERFORMANCE"}
    expected = [
        S('''ALTER TYPE "public"."plan_kind" ADD VALUE 'team' BEFORE 'pro\''''),
        S('ALTER INDEX "public"."account_email" RENAME TO "pgschemadiff_tmpidx_account_email_{x}"'),
        S('ALTER TABLE "public"."account" ADD CONSTRAINT "pgschemadiff_tmpnn_{n}" CHECK("kind" IS NOT NULL) NOT VALID'),
        S('ALTER TABLE "public"."account" VALIDATE CONSTRAINT "pgschemadiff_tmpnn_{n}"'),
        S('ALTER TABLE "public"."account" ALTER COLUMN "kind" SET NOT NULL'),
        S('ALTER TABLE "public"."account" DROP CONSTRAINT "pgschemadiff_tmpnn_{n}"'),
        S('ALTER TABLE "public"."account" DROP COLUMN "created_at"', hazards={"DELETES_DATA"}),
        S("CREATE UNIQUE INDEX CONCURRENTLY account_email ON public.account USING btree (lower(email))", T20M, hazards={"INDEX_BUILD"}),
        S('ALTER TABLE "public"."invoice" DROP CONSTRAINT "invoice_account_id_fkey"'),
        S('ALTER TABLE "public"."invoice" ALTER COLUMN "cents" SET DATA TYPE bigint using "cents"::bigint', hazards=lock),
        S('ANALYZE "public"."invoice" ("cents")', T20M, hazards=perf),
        S("CREATE INDEX CONCURRENTLY invoice_account ON public.invoice USING btree (account_id)", T20M, hazards={"INDEX_BUILD"}),
        S('ALTER TABLE "public"."invoice" ADD CONSTRAINT "invoice_account_id_fkey" FOREIGN KEY (account_id) '
          'REFERENCES account(id) ON DELETE CASCADE NOT VALID'),
        S('ALTER TABLE "public"."invoice" VALIDATE CONSTRAINT "invoice_account_id_fkey"'),
        S('DROP INDEX CONCURRENTLY "public"."pgschemadiff_tmpidx_account_email_{x}"', T20M, hazards={"INDEX_DROPPED"}),
    ]
    assert_plan(actual, expected, order=[(1, 7, 14), (2, 3, 4, 5), (9, 10), (8, 12, 13)])

    result = apply(server, db, ["--to-dir", dirs[2]], "--allow-hazards", ALL_HAZARDS)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_same_catalog(catalog(db), oracle(v3))
    assert db.query("select id, email, kind::text from account order by id") == [(1, "A@example.com", "free"), (2, "b@example.com", "free")]
    assert rows(db, "invoice") == [(10, 1, 500), (11, 2, 0)]
    db.execute("DELETE FROM account WHERE id = 1")
    assert rows(db, "invoice") == [(11, 2, 0)]
    assert plan(["--from-dsn", db.dsn, "--to-dir", dirs[2]], server) == []


def test_recovery_after_failed_validation(server, db, schema_dir, oracle):
    db.execute("CREATE TABLE t(id int, a int); INSERT INTO t VALUES (1, -1), (2, NULL);")
    target = "CREATE TABLE t(id int, a int NOT NULL CONSTRAINT a_pos CHECK (a > 0));"
    directory = schema_dir({"s.sql": target})
    failed = apply(server, db, ["--to-dir", directory], "--allow-hazards", ALL_HAZARDS)
    assert failed.returncode != 0
    # Which independent step fails first is free; every allowed order leaves only unvalidated
    # a_pos and/or temporary NOT NULL checks (at most one of each) and the column still nullable.
    left = db.query("select conname, convalidated from pg_constraint where conrelid = 't'::regclass order by 1")
    assert left, "the completed ADD CONSTRAINT step must remain"
    assert all(validated is False for _, validated in left)
    assert all(name == "a_pos" or name.startswith("pgschemadiff_tmpnn_") for name, _ in left)
    assert sum(name == "a_pos" for name, _ in left) <= 1
    assert sum(name.startswith("pgschemadiff_tmpnn_") for name, _ in left) <= 1
    assert db.scalar("select attnotnull from pg_attribute where attrelid = 't'::regclass and attname = 'a'") is False

    db.execute("UPDATE t SET a = 5")
    fixed = apply(server, db, ["--to-dir", directory], "--allow-hazards", ALL_HAZARDS)
    assert fixed.returncode == 0, fixed.stderr[-2000:]
    assert last_line(fixed.stdout) == "Schema applied successfully"
    assert_same_catalog(catalog(db), oracle(target))
    assert plan(["--from-dsn", db.dsn, "--to-dir", directory], server) == []


def test_invalid_index_left_by_failure_is_rebuilt(server, db, schema_dir, oracle):
    db.execute("CREATE TABLE t(id int, code text); INSERT INTO t VALUES (1, 'x'), (2, 'x');")
    target = "CREATE TABLE t(id int, code text); CREATE UNIQUE INDEX t_code ON t(code);"
    directory = schema_dir({"s.sql": target})
    failed = apply(server, db, ["--to-dir", directory], "--allow-hazards", "INDEX_BUILD")
    assert failed.returncode != 0
    assert db.query("select indexrelid::regclass::text, indisvalid from pg_index where indrelid = 't'::regclass") == [("t_code", False)]

    actual = plan(["--from-dsn", db.dsn, "--to-dir", directory], server)
    expected = [
        S('ALTER INDEX "public"."t_code" RENAME TO "pgschemadiff_tmpidx_t_code_{x}"'),
        S("CREATE UNIQUE INDEX CONCURRENTLY t_code ON public.t USING btree (code)", T20M, hazards={"INDEX_BUILD"}),
        S('DROP INDEX CONCURRENTLY "public"."pgschemadiff_tmpidx_t_code_{x}"', T20M, hazards={"INDEX_DROPPED"}),
    ]
    assert_plan(actual, expected, order=[(0, 1, 2)])

    db.execute("UPDATE t SET code = 'y' WHERE id = 2")
    fixed = apply(server, db, ["--to-dir", directory], "--allow-hazards", "INDEX_BUILD,INDEX_DROPPED")
    assert fixed.returncode == 0, fixed.stderr[-2000:]
    assert_same_catalog(catalog(db), oracle(target))
    assert rows(db, "t") == [(1, "x"), (2, "y")]


DUMPED = """
    CREATE TYPE mood AS ENUM ('a', 'b'); CREATE SEQUENCE s1 START 5;
    CREATE FUNCTION stamp() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.a := coalesce(NEW.a, 0) + 1; RETURN NEW; END $$;
    CREATE TABLE t(id int PRIMARY KEY, a int, b text);
    CREATE INDEX t_a ON t(a, id);
    CREATE TRIGGER tt BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION stamp();
    CREATE TABLE ch(id int REFERENCES t(id), m mood CHECK (m <> 'b'));
"""
DUMP_STATEMENTS = [
    '''CREATE TYPE "public"."mood" AS ENUM ('a', 'b')''',
    "CREATE OR REPLACE FUNCTION public.stamp() RETURNS trigger LANGUAGE plpgsql "
    "AS $function$ BEGIN NEW.a := coalesce(NEW.a, 0) + 1; RETURN NEW; END $function$",
    'CREATE SEQUENCE "public"."s1" AS bigint INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 5 CACHE 1 NO CYCLE',
    'CREATE TABLE "public"."ch" ("id" integer, "m" mood)',
    '''ALTER TABLE "public"."ch" ADD CONSTRAINT "ch_m_check" CHECK((m <> 'b'::mood))''',
    'CREATE TABLE "public"."t" ("id" integer NOT NULL, "a" integer, "b" text COLLATE "pg_catalog"."default")',
    "CREATE UNIQUE INDEX t_pkey ON public.t USING btree (id)",
    'ALTER TABLE "public"."t" ADD CONSTRAINT "t_pkey" PRIMARY KEY USING INDEX "t_pkey"',
    "CREATE INDEX t_a ON public.t USING btree (a, id)",
    'ALTER TABLE "public"."ch" ADD CONSTRAINT "ch_id_fkey" FOREIGN KEY (id) REFERENCES t(id) NOT VALID',
    'ALTER TABLE "public"."ch" VALIDATE CONSTRAINT "ch_id_fkey"',
    "CREATE TRIGGER tt BEFORE INSERT ON public.t FOR EACH ROW EXECUTE FUNCTION stamp()",
]
DUMP_ORDER = [(0, 3, 4), (1, 11), (5, 6, 7, 9, 10), (3, 9), (5, 8), (5, 11)]


def test_dump_prints_executable_ddl(make_db):
    source = make_db(DUMPED)
    result = run_program(["dump", "--dsn", source.dsn])
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith(";")
    assert "CONCURRENTLY" not in result.stdout and "statement_timeout" not in result.stdout
    actual = [{"ddl": d, "timeout": 0, "lock": 0, "hazards": frozenset()} for d in split_dump(result.stdout)]
    assert_plan(actual, [S(d, 0, 0) for d in DUMP_STATEMENTS], order=DUMP_ORDER)


def test_dump_schema_filters(make_db):
    source = make_db("CREATE SCHEMA other; CREATE TABLE other.o(id int); CREATE TABLE pub(id int);")
    only_other = run_program(["dump", "--dsn", source.dsn, "--exclude-schema", "public"])
    assert only_other.returncode == 0, only_other.stderr
    assert split_dump(only_other.stdout) == [S('CREATE SCHEMA "other"').ddl, S('CREATE TABLE "other"."o" ("id" integer)').ddl]
    only_public = run_program(["dump", "--dsn", source.dsn, "--include-schema", "public"])
    assert only_public.returncode == 0, only_public.stderr
    assert split_dump(only_public.stdout) == [S('CREATE TABLE "public"."pub" ("id" integer)').ddl]


def test_dump_round_trip_through_plan_and_apply(server, make_db, schema_dir):
    source = make_db(DUMPED)
    dumped = run_program(["dump", "--dsn", source.dsn])
    assert dumped.returncode == 0, dumped.stderr
    directory = schema_dir({"dump.sql": dumped.stdout})
    fresh = make_db()
    actual = plan(["--from-dsn", fresh.dsn, "--to-dir", directory], server)
    concurrent = {"CREATE UNIQUE INDEX t_pkey": "CREATE UNIQUE INDEX CONCURRENTLY t_pkey", "CREATE INDEX t_a": "CREATE INDEX CONCURRENTLY t_a"}
    expected = []
    for ddl in DUMP_STATEMENTS:
        if ddl.startswith(("CREATE UNIQUE INDEX", "CREATE INDEX")):
            ddl = next(new + ddl[len(old):] for old, new in concurrent.items() if ddl.startswith(old))
            expected.append(S(ddl, T20M))
        elif ddl.startswith(("CREATE SEQUENCE", "CREATE OR REPLACE FUNCTION")):
            expected.append(S(ddl, hazards={"HAS_UNTRACKABLE_DEPENDENCIES"}))
        else:
            expected.append(S(ddl))
    assert_plan(actual, expected, order=DUMP_ORDER)
    result = apply(server, fresh, ["--to-dir", directory], "--allow-hazards", "HAS_UNTRACKABLE_DEPENDENCIES")
    assert result.returncode == 0, result.stderr[-2000:]
    assert_same_catalog(catalog(fresh), catalog(source))
    assert plan(["--from-dsn", fresh.dsn, "--to-dsn", source.dsn], server) == []


def test_nested_schema_directories(server, db, schema_dir, oracle):
    # Adapted from upstream testdata dirsrc_happy_path (schema_0 nested files, schema_1).
    schema_0 = schema_dir({
        "0/0.sql": "CREATE TYPE color AS ENUM ('red', 'green', 'blue');",
        "1.sql": "CREATE TABLE foobar (\n    id varchar(255) PRIMARY KEY,\n    color color\n);",
        "2/0/0.sql": "CREATE TABLE foobar_fk (\n    id TEXT REFERENCES foobar (id)\n);",
    })
    schema_1 = schema_dir({"0.sql": "CREATE TABLE fizzbuzz (\n    id TEXT,\n    primary_color COLOR,\n    other_color COLOR\n);"})
    db.execute("CREATE TABLE foobar();")
    actual = plan(["--from-dsn", db.dsn, "--to-dir", schema_0, "--to-dir", schema_1], server)
    hazards = set().union(*(s["hazards"] for s in actual))
    assert hazards == {"INDEX_BUILD"}
    result = apply(server, db, ["--to-dir", schema_0, "--to-dir", schema_1], "--allow-hazards", "INDEX_BUILD")
    assert result.returncode == 0, result.stderr[-2000:]
    assert_same_catalog(catalog(db), oracle("""
        CREATE TYPE color AS ENUM ('red', 'green', 'blue');
        CREATE TABLE foobar(id varchar(255) PRIMARY KEY, color color);
        CREATE TABLE foobar_fk(id TEXT REFERENCES foobar(id));
        CREATE TABLE fizzbuzz(id TEXT, primary_color color, other_color color);"""))
