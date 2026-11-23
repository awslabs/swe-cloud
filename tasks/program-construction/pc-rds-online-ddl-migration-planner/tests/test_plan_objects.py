"""Plans for sequences, enum types, functions and triggers (adapted from upstream acceptance cases)."""

from planlib import S, T20M, assert_plan, plan_change

UNTRACKABLE = {"HAS_UNTRACKABLE_DEPENDENCIES"}
FN_ADD = "CREATE FUNCTION add(a integer, b integer) RETURNS integer LANGUAGE SQL IMMUTABLE RETURNS NULL ON NULL INPUT RETURN a + b;\n"
FN_INCR = "CREATE FUNCTION incr(i integer) RETURNS integer AS $$ BEGIN RETURN i + 1; END; $$ LANGUAGE plpgsql;\n"
TRG_FN = "CREATE FUNCTION stamp() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.v := coalesce(NEW.v, 0) + 1; RETURN NEW; END $$;\n"
TRG_FN2 = "CREATE FUNCTION stamp2() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.v := 100; RETURN NEW; END $$;\n"


def test_create_sequences(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int);", """
        CREATE TABLE t(id int);
        CREATE SEQUENCE seq_a AS integer START 100 INCREMENT 5 MAXVALUE 1000 CACHE 2 CYCLE;
        CREATE SEQUENCE seq_b;""")
    assert_plan(actual, [
        S('''CREATE SEQUENCE "public"."seq_a" AS integer INCREMENT BY 5 MINVALUE 1 MAXVALUE 1000
             START WITH 100 CACHE 2 CYCLE''', hazards=UNTRACKABLE),
        S('''CREATE SEQUENCE "public"."seq_b" AS bigint INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807
             START WITH 1 CACHE 1 NO CYCLE''', hazards=UNTRACKABLE),
    ])


def test_alter_and_drop_sequences(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, """
        CREATE SEQUENCE s AS integer INCREMENT BY 2 MINVALUE 5 MAXVALUE 100 START WITH 10 CACHE 5 CYCLE;
        CREATE SEQUENCE s2; SELECT nextval('s');""",
        "CREATE SEQUENCE s AS bigint INCREMENT BY 3 MINVALUE 5 MAXVALUE 500 START WITH 10 CACHE 5 NO CYCLE;")
    assert_plan(actual, [
        S('ALTER SEQUENCE "public"."s" AS bigint INCREMENT BY 3 MINVALUE 5 MAXVALUE 500 START WITH 10 CACHE 5 NO CYCLE'),
        S('DROP SEQUENCE "public"."s2"', hazards={"DELETES_DATA", "HAS_UNTRACKABLE_DEPENDENCIES"}),
    ])


def test_sequence_ownership(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int, n bigint); CREATE SEQUENCE s;",
                         "CREATE TABLE t(id int, n bigint); CREATE SEQUENCE s OWNED BY t.n;")
    assert_plan(actual, [S('ALTER SEQUENCE "public"."s" OWNED BY "public"."t"."n"')])


def test_create_enum_before_its_uses(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int);",
                         "CREATE TYPE color AS ENUM ('red', 'green'); CREATE TABLE t(id int, c color DEFAULT 'red'); CREATE TABLE t2(c color NOT NULL);")
    expected = [
        S('''CREATE TYPE "public"."color" AS ENUM ('red', 'green')'''),
        S('''ALTER TABLE "public"."t" ADD COLUMN "c" color DEFAULT 'red'::color'''),
        S('CREATE TABLE "public"."t2" ("c" color NOT NULL)'),
    ]
    assert_plan(actual, expected, order=[(0, 1), (0, 2)])


def test_enum_values_added_in_place(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TYPE e AS ENUM ('1', '2', '3'); CREATE TABLE foo(val e); INSERT INTO foo VALUES ('2');",
                         "CREATE TYPE e AS ENUM ('0', '1', '1.5', '2', '2.5', '3', '4'); CREATE TABLE foo(val e DEFAULT '1.5');")
    expected = [
        S('''ALTER TYPE "public"."e" ADD VALUE '4\''''),
        S('''ALTER TYPE "public"."e" ADD VALUE '2.5' BEFORE '3\''''),
        S('''ALTER TYPE "public"."e" ADD VALUE '1.5' BEFORE '2\''''),
        S('''ALTER TYPE "public"."e" ADD VALUE '0' BEFORE '1\''''),
        S('''ALTER TABLE "public"."foo" ALTER COLUMN "val" SET DEFAULT '1.5'::e'''),
    ]
    assert_plan(actual, expected, order=[(2, 4)])


def test_enum_value_removal_recreates_unused_type(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TYPE e AS ENUM ('a', 'b', 'c');", "CREATE TYPE e AS ENUM ('a', 'c', 'd');")
    expected = [S('DROP TYPE "public"."e"'), S('''CREATE TYPE "public"."e" AS ENUM ('a', 'c', 'd')''')]
    assert_plan(actual, expected, order=[(0, 1)])


def test_drop_enum_after_its_column(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TYPE e AS ENUM ('a'); CREATE TABLE foo(id int, val e);", "CREATE TABLE foo(id int);")
    expected = [S('ALTER TABLE "public"."foo" DROP COLUMN "val"', hazards={"DELETES_DATA"}), S('DROP TYPE "public"."e"')]
    assert_plan(actual, expected, order=[(0, 1)])


def test_create_functions_in_dependency_order(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "", FN_ADD + FN_INCR + """
        CREATE FUNCTION add(a text, b text) RETURNS text LANGUAGE SQL IMMUTABLE RETURN concat(a, b);
        CREATE FUNCTION both_(a integer) RETURNS integer LANGUAGE SQL RETURN add(a, a) + incr(a);""")
    expected = [
        S("CREATE OR REPLACE FUNCTION public.add(a integer, b integer) RETURNS integer LANGUAGE sql IMMUTABLE STRICT RETURN (a + b)"),
        S("CREATE OR REPLACE FUNCTION public.add(a text, b text) RETURNS text LANGUAGE sql IMMUTABLE RETURN concat(a, b)"),
        S("CREATE OR REPLACE FUNCTION public.incr(i integer) RETURNS integer LANGUAGE plpgsql "
          "AS $function$ BEGIN RETURN i + 1; END; $function$", hazards=UNTRACKABLE),
        S("CREATE OR REPLACE FUNCTION public.both_(a integer) RETURNS integer LANGUAGE sql RETURN (add(a, a) + incr(a))"),
    ]
    assert_plan(actual, expected, order=[(0, 3), (2, 3)])


def test_alter_functions(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, FN_ADD + FN_INCR,
                         FN_ADD.replace("a + b", "a + b + 1") + FN_INCR.replace("i + 1", "i + 2"))
    assert_plan(actual, [
        S("CREATE OR REPLACE FUNCTION public.add(a integer, b integer) RETURNS integer LANGUAGE sql IMMUTABLE STRICT RETURN ((a + b) + 1)"),
        S("CREATE OR REPLACE FUNCTION public.incr(i integer) RETURNS integer LANGUAGE plpgsql "
          "AS $function$ BEGIN RETURN i + 2; END; $function$", hazards=UNTRACKABLE),
    ])


def test_drop_dependent_function_first(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         FN_ADD + FN_INCR + "CREATE FUNCTION dep(a integer) RETURNS integer LANGUAGE SQL RETURN add(a, 1);", FN_INCR)
    expected = [S('DROP FUNCTION "public"."dep"(a integer)'), S('DROP FUNCTION "public"."add"(a integer, b integer)')]
    assert_plan(actual, expected, order=[(0, 1)])


def test_switch_function_dependency(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE FUNCTION a1(x int) RETURNS int LANGUAGE SQL RETURN x + 1; CREATE FUNCTION b1(x int) RETURNS int LANGUAGE SQL RETURN a1(x) * 2;",
                         "CREATE FUNCTION a2(x int) RETURNS int LANGUAGE SQL RETURN x + 10; CREATE FUNCTION b1(x int) RETURNS int LANGUAGE SQL RETURN a2(x) * 2;")
    expected = [
        S("CREATE OR REPLACE FUNCTION public.a2(x integer) RETURNS integer LANGUAGE sql RETURN (x + 10)"),
        S("CREATE OR REPLACE FUNCTION public.b1(x integer) RETURNS integer LANGUAGE sql RETURN (a2(x) * 2)"),
        S('DROP FUNCTION "public"."a1"(x integer)'),
    ]
    assert_plan(actual, expected, order=[(0, 1, 2)])


def test_create_trigger_after_its_function(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int, v int);", "CREATE TABLE t(id int, v int);" + TRG_FN +
                         "CREATE TRIGGER t_stamp BEFORE INSERT OR UPDATE ON t FOR EACH ROW WHEN (NEW.id > 0) EXECUTE FUNCTION stamp();")
    expected = [
        S("CREATE OR REPLACE FUNCTION public.stamp() RETURNS trigger LANGUAGE plpgsql "
          "AS $function$ BEGIN NEW.v := coalesce(NEW.v, 0) + 1; RETURN NEW; END $function$", hazards=UNTRACKABLE),
        S("CREATE TRIGGER t_stamp BEFORE INSERT OR UPDATE ON public.t FOR EACH ROW WHEN ((new.id > 0)) EXECUTE FUNCTION stamp()"),
    ]
    assert_plan(actual, expected, order=[(0, 1)])


def test_alter_trigger_in_place(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE t(id int, v int);" + TRG_FN + TRG_FN2 + "CREATE TRIGGER t_stamp BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION stamp();",
                         "CREATE TABLE t(id int, v int);" + TRG_FN + TRG_FN2 +
                         "CREATE TRIGGER t_stamp BEFORE INSERT ON t FOR EACH ROW WHEN (NEW.id <> 0) EXECUTE FUNCTION stamp2();")
    assert_plan(actual, [S("CREATE OR REPLACE TRIGGER t_stamp BEFORE INSERT ON public.t FOR EACH ROW WHEN ((new.id <> 0)) EXECUTE FUNCTION stamp2()")])


def test_change_trigger_function_and_drop_old(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, """
        CREATE TABLE t(id int, v int);
        CREATE FUNCTION f1() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.v := 1; RETURN NEW; END $$;
        CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION f1();""", """
        CREATE TABLE t(id int, v int);
        CREATE FUNCTION f2() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.v := 2; RETURN NEW; END $$;
        CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION f2();""")
    expected = [
        S("CREATE OR REPLACE FUNCTION public.f2() RETURNS trigger LANGUAGE plpgsql "
          "AS $function$ BEGIN NEW.v := 2; RETURN NEW; END $function$", hazards=UNTRACKABLE),
        S("CREATE OR REPLACE TRIGGER trg BEFORE INSERT ON public.t FOR EACH ROW EXECUTE FUNCTION f2()"),
        S('DROP FUNCTION "public"."f1"()', hazards=UNTRACKABLE),
    ]
    assert_plan(actual, expected, order=[(0, 1, 2)])


def test_triggers_on_new_table(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "", """
        CREATE FUNCTION f1() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.v := 1; RETURN NEW; END $$;
        CREATE TABLE t(id int, v int);
        CREATE TRIGGER trg AFTER UPDATE ON t FOR EACH ROW EXECUTE FUNCTION f1();
        CREATE TRIGGER trg2 BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION f1();""")
    expected = [
        S("CREATE OR REPLACE FUNCTION public.f1() RETURNS trigger LANGUAGE plpgsql "
          "AS $function$ BEGIN NEW.v := 1; RETURN NEW; END $function$", hazards=UNTRACKABLE),
        S('CREATE TABLE "public"."t" ("id" integer, "v" integer)'),
        S("CREATE TRIGGER trg AFTER UPDATE ON public.t FOR EACH ROW EXECUTE FUNCTION f1()"),
        S("CREATE TRIGGER trg2 BEFORE INSERT ON public.t FOR EACH ROW EXECUTE FUNCTION f1()"),
    ]
    assert_plan(actual, expected, order=[(0, 2), (1, 2), (0, 3), (1, 3)])


def test_drop_trigger_before_its_function(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE t(id int, v int);" + TRG_FN + "CREATE TRIGGER t_stamp BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION stamp();",
                         "CREATE TABLE t(id int, v int);")
    expected = [S('DROP TRIGGER "t_stamp" ON "public"."t"'), S('DROP FUNCTION "public"."stamp"()', hazards=UNTRACKABLE)]
    assert_plan(actual, expected, order=[(0, 1)])


def test_drop_everything_in_reverse_dependency_order(server, make_db):
    from planlib import plan
    full = make_db("""
        CREATE TYPE mood AS ENUM ('a', 'b'); CREATE SEQUENCE s1 START 5;
        CREATE FUNCTION stamp() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.a := coalesce(NEW.a, 0) + 1; RETURN NEW; END $$;
        CREATE TABLE t(id int PRIMARY KEY, a int);
        CREATE TRIGGER tt BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION stamp();
        CREATE TABLE ch(id int REFERENCES t(id), m mood CHECK (m <> 'b'));""")
    empty = make_db()
    actual = plan(["--from-dsn", full.dsn, "--to-dsn", empty.dsn], server)
    expected = [
        S('ALTER TABLE "public"."ch" DROP CONSTRAINT "ch_id_fkey"'),
        S('DROP SEQUENCE "public"."s1"', hazards={"DELETES_DATA", "HAS_UNTRACKABLE_DEPENDENCIES"}),
        S('DROP TABLE "public"."ch"', T20M, hazards={"DELETES_DATA"}),
        S('DROP TRIGGER "tt" ON "public"."t"'),
        S('DROP FUNCTION "public"."stamp"()', hazards=UNTRACKABLE),
        S('DROP TABLE "public"."t"', T20M, hazards={"DELETES_DATA"}),
        S('DROP TYPE "public"."mood"'),
    ]
    assert_plan(actual, expected, order=[(0, 5), (3, 4), (3, 5), (2, 6)])
