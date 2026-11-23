"""Plans for single table and column changes (adapted from upstream table/column acceptance cases)."""

from planlib import S, T20M, assert_plan, plan_change


def test_create_table_is_data_packed(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "", """
        CREATE TABLE events(id int PRIMARY KEY, note text, created_at timestamptz NOT NULL DEFAULT now(),
            flag boolean DEFAULT false, big bigint, small smallint, amount numeric(10,2), code char(3), uid uuid,
            CHECK (big > small));
    """)
    expected = [
        S('''CREATE TABLE "public"."events" (
            "uid" uuid,
            "created_at" timestamp with time zone DEFAULT now() NOT NULL,
            "big" bigint,
            "id" integer NOT NULL,
            "small" smallint,
            "flag" boolean DEFAULT false,
            "note" text COLLATE "pg_catalog"."default",
            "amount" numeric(10,2),
            "code" character(3) COLLATE "pg_catalog"."default"
        )'''),
        S('ALTER TABLE "public"."events" ADD CONSTRAINT "events_check" CHECK((big > small))'),
        S("CREATE UNIQUE INDEX CONCURRENTLY events_pkey ON public.events USING btree (id)", T20M),
        S('ALTER TABLE "public"."events" ADD CONSTRAINT "events_pkey" PRIMARY KEY USING INDEX "events_pkey"'),
    ]
    assert_plan(actual, expected, order=[(0, 1), (0, 2, 3)])


def test_create_table_without_data_packing_keeps_declared_order(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "",
                         "CREATE TABLE fresh(x int, note text, y bigint, flag boolean);",
                         args=["--data-pack-new-tables=false"])
    assert_plan(actual, [S('''CREATE TABLE "public"."fresh" (
        "x" integer, "note" text COLLATE "pg_catalog"."default", "y" bigint, "flag" boolean)''')])


def test_drop_table_deletes_data(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE keep(id int); CREATE TABLE gone(id int PRIMARY KEY, v text); INSERT INTO gone VALUES (1, 'x');",
                         "CREATE TABLE keep(id int);")
    assert_plan(actual, [S('DROP TABLE "public"."gone"', T20M, hazards={"DELETES_DATA"})])


def test_add_columns_with_defaults_and_serial(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE t(id int PRIMARY KEY); INSERT INTO t VALUES (1);",
                         "CREATE TABLE t(id int PRIMARY KEY, a text, b int DEFAULT 5, c int NOT NULL DEFAULT 7, d text NOT NULL, e bigserial);")
    expected = [
        S('''CREATE SEQUENCE "public"."t_e_seq"
            AS bigint
            INCREMENT BY 1
            MINVALUE 1 MAXVALUE 9223372036854775807
            START WITH 1 CACHE 1 NO CYCLE'''),
        S('ALTER TABLE "public"."t" ADD COLUMN "a" text COLLATE "pg_catalog"."default"'),
        S('ALTER TABLE "public"."t" ADD COLUMN "b" integer DEFAULT 5'),
        S('ALTER TABLE "public"."t" ADD COLUMN "c" integer DEFAULT 7 NOT NULL'),
        S('ALTER TABLE "public"."t" ADD COLUMN "d" text COLLATE "pg_catalog"."default" NOT NULL'),
        S('''ALTER TABLE "public"."t" ADD COLUMN "e" bigint DEFAULT nextval('t_e_seq'::regclass) NOT NULL'''),
        S('ALTER SEQUENCE "public"."t_e_seq" OWNED BY "public"."t"."e"'),
    ]
    assert_plan(actual, expected, order=[(0, 5, 6)])


def test_drop_column_deletes_data(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int PRIMARY KEY, a text, b int);",
                         "CREATE TABLE t(id int PRIMARY KEY, b int);")
    assert_plan(actual, [S('ALTER TABLE "public"."t" DROP COLUMN "a"', hazards={"DELETES_DATA"})])


def test_drop_serial_column(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id serial, v int);", "CREATE TABLE t(v int);")
    assert_plan(actual, [S('ALTER TABLE "public"."t" DROP COLUMN "id"', hazards={"DELETES_DATA"})])


def test_column_type_changes_are_followed_by_analyze(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE t(id int PRIMARY KEY, n int, v varchar(20), ts bigint, d numeric(5,1));",
                         "CREATE TABLE t(id int PRIMARY KEY, n bigint, v text, ts timestamp, d numeric(8,2));")
    lock = {"ACQUIRES_ACCESS_EXCLUSIVE_LOCK"}
    perf = {"IMPACTS_DATABASE_PERFORMANCE"}
    expected = [
        S('ALTER TABLE "public"."t" ALTER COLUMN "d" SET DATA TYPE numeric(8,2) using "d"::numeric(8,2)', hazards=lock),
        S('ANALYZE "public"."t" ("d")', T20M, hazards=perf),
        S('ALTER TABLE "public"."t" ALTER COLUMN "n" SET DATA TYPE bigint using "n"::bigint', hazards=lock),
        S('ANALYZE "public"."t" ("n")', T20M, hazards=perf),
        S('ALTER TABLE "public"."t" ALTER COLUMN "ts" SET DATA TYPE timestamp without time zone using '
          'pg_catalog.to_timestamp("ts"::pg_catalog.float8 OPERATOR(pg_catalog./) 1000.0::pg_catalog.float8)', hazards=lock),
        S('ANALYZE "public"."t" ("ts")', T20M, hazards=perf),
        S('ALTER TABLE "public"."t" ALTER COLUMN "v" SET DATA TYPE text COLLATE "pg_catalog"."default" using "v"::text', hazards=lock),
        S('ANALYZE "public"."t" ("v")', T20M, hazards=perf),
    ]
    assert_plan(actual, expected, order=[(0, 1), (2, 3), (4, 5), (6, 7)])


def test_column_defaults(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE t(id int PRIMARY KEY, a int, b int DEFAULT 1, c text DEFAULT 'x');",
                         "CREATE TABLE t(id int PRIMARY KEY, a int DEFAULT 42, b int, c text DEFAULT 'y');")
    assert_plan(actual, [
        S('ALTER TABLE "public"."t" ALTER COLUMN "a" SET DEFAULT 42'),
        S('ALTER TABLE "public"."t" ALTER COLUMN "b" DROP DEFAULT'),
        S('''ALTER TABLE "public"."t" ALTER COLUMN "c" SET DEFAULT 'y'::text'''),
    ])


def test_nullability_uses_temporary_check(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE t(id int PRIMARY KEY, a int, b int NOT NULL); INSERT INTO t VALUES (1, 1, 1);",
                         "CREATE TABLE t(id int PRIMARY KEY, a int NOT NULL, b int);")
    expected = [
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "pgschemadiff_tmpnn_{n}" CHECK("a" IS NOT NULL) NOT VALID'),
        S('ALTER TABLE "public"."t" VALIDATE CONSTRAINT "pgschemadiff_tmpnn_{n}"'),
        S('ALTER TABLE "public"."t" ALTER COLUMN "a" SET NOT NULL'),
        S('ALTER TABLE "public"."t" ALTER COLUMN "b" DROP NOT NULL'),
        S('ALTER TABLE "public"."t" DROP CONSTRAINT "pgschemadiff_tmpnn_{n}"'),
    ]
    assert_plan(actual, expected, order=[(0, 1, 2, 4)])


def test_type_nullability_and_default_together(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int PRIMARY KEY, a int); INSERT INTO t VALUES (1, 3);",
                         "CREATE TABLE t(id int PRIMARY KEY, a bigint NOT NULL DEFAULT 9);")
    expected = [
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "pgschemadiff_tmpnn_{n}" CHECK("a" IS NOT NULL) NOT VALID'),
        S('ALTER TABLE "public"."t" VALIDATE CONSTRAINT "pgschemadiff_tmpnn_{n}"'),
        S('ALTER TABLE "public"."t" ALTER COLUMN "a" SET NOT NULL'),
        S('ALTER TABLE "public"."t" ALTER COLUMN "a" SET DATA TYPE bigint using "a"::bigint', hazards={"ACQUIRES_ACCESS_EXCLUSIVE_LOCK"}),
        S('ANALYZE "public"."t" ("a")', T20M, hazards={"IMPACTS_DATABASE_PERFORMANCE"}),
        S('ALTER TABLE "public"."t" ALTER COLUMN "a" SET DEFAULT 9'),
        S('ALTER TABLE "public"."t" DROP CONSTRAINT "pgschemadiff_tmpnn_{n}"'),
    ]
    assert_plan(actual, expected, order=[(0, 1, 2, 6), (3, 4), (3, 5)])


def test_bigint_to_timestamp_not_null_with_default(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int, ts bigint); INSERT INTO t VALUES (1, 0);",
                         "CREATE TABLE t(id int, ts timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP);")
    expected = [
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "pgschemadiff_tmpnn_{n}" CHECK("ts" IS NOT NULL) NOT VALID'),
        S('ALTER TABLE "public"."t" VALIDATE CONSTRAINT "pgschemadiff_tmpnn_{n}"'),
        S('ALTER TABLE "public"."t" ALTER COLUMN "ts" SET NOT NULL'),
        S('ALTER TABLE "public"."t" ALTER COLUMN "ts" SET DATA TYPE timestamp without time zone using '
          'pg_catalog.to_timestamp("ts"::pg_catalog.float8 OPERATOR(pg_catalog./) 1000.0::pg_catalog.float8)',
          hazards={"ACQUIRES_ACCESS_EXCLUSIVE_LOCK"}),
        S('ANALYZE "public"."t" ("ts")', T20M, hazards={"IMPACTS_DATABASE_PERFORMANCE"}),
        S('ALTER TABLE "public"."t" ALTER COLUMN "ts" SET DEFAULT CURRENT_TIMESTAMP'),
        S('ALTER TABLE "public"."t" DROP CONSTRAINT "pgschemadiff_tmpnn_{n}"'),
    ]
    assert_plan(actual, expected, order=[(0, 1, 2, 6), (3, 4), (3, 5)])


def test_identical_schema_and_column_order_only_give_empty_plans(server, db, schema_dir):
    ddl = """CREATE TYPE m AS ENUM ('a'); CREATE SEQUENCE s;
             CREATE TABLE t(id int PRIMARY KEY, m m DEFAULT 'a', n bigint DEFAULT nextval('s') CHECK (n > 0));
             CREATE INDEX t_n ON t(n) WHERE n > 5;
             CREATE TABLE pair(a int, b int);"""
    actual = plan_change(server, db, schema_dir, ddl, ddl.replace("pair(a int, b int)", "pair(b int, a int)"))
    assert actual == []
