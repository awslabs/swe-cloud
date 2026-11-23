"""apply: the hazard gate, catalog effects, data preservation and failure effects."""

from planlib import ALL_HAZARDS, apply, assert_same_catalog, catalog, column_order, last_line, plan, rows

OLD = """CREATE TABLE t(id int PRIMARY KEY, a int); CREATE INDEX t_a ON t(a); CREATE TABLE legacy(id int);
         INSERT INTO t VALUES (1, 10), (2, 20); INSERT INTO legacy VALUES (7);"""
NEW = "CREATE TABLE t(id int PRIMARY KEY, a int, b text); CREATE INDEX t_a ON t(a, id); CREATE TABLE fresh(x int, y bigint);"


def test_refused_hazards_leave_database_untouched(server, db, schema_dir):
    db.execute(OLD)
    directory = schema_dir({"s.sql": NEW})
    snapshot = catalog(db)
    refused = apply(server, db, ["--to-dir", directory])
    assert refused.returncode != 0
    assert "Schema applied successfully" not in refused.stdout
    for hazard in ("INDEX_BUILD", "INDEX_DROPPED", "DELETES_DATA"):
        assert hazard in refused.stderr
    assert catalog(db) == snapshot
    assert rows(db, "legacy") == [(7,)] and rows(db, "t") == [(1, 10), (2, 20)]

    partial = apply(server, db, ["--to-dir", directory], "--allow-hazards", "index_build", "--allow-hazards", "DELETES_DATA")
    assert partial.returncode != 0
    assert "INDEX_DROPPED" in partial.stderr
    assert catalog(db) == snapshot
    assert rows(db, "legacy") == [(7,)]


def test_allowed_hazards_are_case_insensitive_and_combinable(server, db, schema_dir, oracle):
    db.execute(OLD)
    directory = schema_dir({"s.sql": NEW})
    result = apply(server, db, ["--to-dir", directory], "--allow-hazards", "index_build,deletes_data,INDEX_DROPPED,INDEX_BUILD")
    assert result.returncode == 0, result.stderr[-2000:]
    assert last_line(result.stdout) == "Schema applied successfully"
    assert_same_catalog(catalog(db), oracle(NEW))
    assert rows(db, "t") == [(1, 10, None), (2, 20, None)]
    assert column_order(db, "fresh") == ["y", "x"]
    assert db.scalar("select count(*) from pg_class where relname like 'pgschemadiff%'") == 0
    assert plan(["--from-dsn", db.dsn, "--to-dir", directory], server) == []
    again = apply(server, db, ["--to-dir", directory])
    assert again.returncode == 0, again.stderr
    assert last_line(again.stdout) == "Schema matches expected. No plan generated"


def test_hazard_free_plan_needs_no_allowance(server, db, schema_dir, oracle):
    old = "CREATE TABLE t(id int PRIMARY KEY, a int DEFAULT 1); INSERT INTO t VALUES (1, 5);"
    new = "CREATE TABLE t(id int PRIMARY KEY, a int DEFAULT 2, note text NOT NULL DEFAULT 'n/a', CONSTRAINT a_pos CHECK (a > 0));"
    db.execute(old)
    result = apply(server, db, ["--to-dir", schema_dir({"s.sql": new})])
    assert result.returncode == 0, result.stderr[-2000:]
    assert last_line(result.stdout) == "Schema applied successfully"
    assert_same_catalog(catalog(db), oracle(new))
    assert rows(db, "t") == [(1, 5, "n/a")]


def test_apply_builds_valid_objects(server, db, schema_dir, oracle):
    old = """CREATE TABLE foobar(id int PRIMARY KEY, created_at timestamp, message text);
             CREATE INDEX message_idx ON foobar(message);
             CREATE TABLE legacy(id int);
             INSERT INTO foobar(id, created_at, message) VALUES (1, '2024-01-02 03:04:05', 'hello'); INSERT INTO legacy VALUES (7);"""
    files = {
        "00_types.sql": "CREATE TYPE mood AS ENUM ('happy', 'sad');\nCREATE SEQUENCE ticket_seq START 100;\n",
        "10_tables.sql": """
            CREATE TABLE foobar(id int PRIMARY KEY, created_at timestamp NOT NULL, message text, m mood);
            CREATE INDEX message_idx ON foobar(message, created_at);
            CREATE TABLE child(id int PRIMARY KEY, foobar_id int REFERENCES foobar(id), qty int CHECK (qty > 0));
            CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.qty := coalesce(NEW.qty, 1); RETURN NEW; END $$;
            CREATE TRIGGER child_touch BEFORE INSERT ON child FOR EACH ROW EXECUTE FUNCTION touch();
        """,
    }
    db.execute(old)
    directory = schema_dir(files)
    refused = apply(server, db, ["--to-dir", directory], "--allow-hazards", "DELETES_DATA,INDEX_BUILD,INDEX_DROPPED")
    assert refused.returncode != 0 and "HAS_UNTRACKABLE_DEPENDENCIES" in refused.stderr
    assert db.scalar("select to_regclass('public.child') is null") is True
    result = apply(server, db, ["--to-dir", directory], "--allow-hazards", "DELETES_DATA,HAS_UNTRACKABLE_DEPENDENCIES,INDEX_BUILD,INDEX_DROPPED")
    assert result.returncode == 0, result.stderr[-2000:]
    assert last_line(result.stdout) == "Schema applied successfully"
    assert_same_catalog(catalog(db), oracle(files["00_types.sql"] + files["10_tables.sql"]))
    assert db.query("select message, created_at::text, m from foobar") == [("hello", "2024-01-02 03:04:05", None)]
    assert db.query("insert into child(id, foobar_id) values (5, 1) returning qty") == [(1,)]
    assert db.scalar("select nextval('ticket_seq')") == 100
    assert db.scalar("select count(*) from pg_constraint where conname like 'pgschemadiff%'") == 0
    assert column_order(db, "child") == ["id", "foobar_id", "qty"]


def test_apply_to_match_another_database(server, make_db, oracle):
    source = make_db("CREATE TABLE t(id int PRIMARY KEY, a int); INSERT INTO t VALUES (1, 2); CREATE TABLE gone(id int);")
    target_ddl = "CREATE TABLE t(id int PRIMARY KEY, a int, b text DEFAULT 'z'); CREATE INDEX t_b ON t(b); CREATE TABLE u(x int, n bigint);"
    target = make_db(target_ddl)
    target_before = catalog(target)
    refused = apply(server, source, ["--to-dsn", target.dsn], "--allow-hazards", "INDEX_BUILD")
    assert refused.returncode != 0 and "DELETES_DATA" in refused.stderr
    result = apply(server, source, ["--to-dsn", target.dsn], "--allow-hazards", "INDEX_BUILD,DELETES_DATA")
    assert result.returncode == 0, result.stderr[-2000:]
    assert_same_catalog(catalog(source), oracle(target_ddl))
    assert column_order(source, "u") == ["n", "x"]
    assert rows(source, "t") == [(1, 2, "z")]
    assert catalog(target) == target_before


def test_inserted_statement_is_gated_and_executed(server, db, schema_dir):
    db.execute("CREATE TABLE t(id int); INSERT INTO t VALUES (1), (2);")
    directory = schema_dir({"s.sql": "CREATE TABLE t(id int, v int DEFAULT 0);"})
    insert = ["-s", 'index=1 statement="UPDATE t SET v = id * 10" timeout=10s lock_timeout=1s']
    refused = apply(server, db, ["--to-dir", directory], *insert)
    assert refused.returncode != 0 and "IS_USER_GENERATED" in refused.stderr
    assert db.query("select attname from pg_attribute where attrelid = 't'::regclass and attnum > 0") == [("id",)]
    result = apply(server, db, ["--to-dir", directory], *insert, "--allow-hazards", "is_user_generated")
    assert result.returncode == 0, result.stderr[-2000:]
    assert rows(db, "t") == [(1, 10), (2, 20)]


def test_failed_statement_keeps_completed_steps(server, db, schema_dir):
    db.execute("CREATE TABLE t(id int, a int, code text); INSERT INTO t VALUES (1, -1, 'x'), (2, NULL, 'x');")
    checked = schema_dir({"s.sql": "CREATE TABLE t(id int, a int CONSTRAINT a_pos CHECK (a > 0), code text);"})
    result = apply(server, db, ["--to-dir", checked], "--allow-hazards", ALL_HAZARDS)
    assert result.returncode != 0
    assert "Schema applied successfully" not in result.stdout
    assert db.query("select conname, convalidated from pg_constraint where conrelid = 't'::regclass") == [("a_pos", False)]
    assert rows(db, "t") == [(1, -1, "x"), (2, None, "x")]

    # Start the second failure from a table without the leftover constraint, so the plan is a single index build.
    db.execute("ALTER TABLE t DROP CONSTRAINT a_pos")
    unique = schema_dir({"s.sql": "CREATE TABLE t(id int, a int, code text); CREATE UNIQUE INDEX t_code ON t(code);"})
    result = apply(server, db, ["--to-dir", unique], "--allow-hazards", ALL_HAZARDS)
    assert result.returncode != 0
    assert db.query("select conname from pg_constraint where conrelid = 't'::regclass") == []
    assert db.query("select indexrelid::regclass::text, indisvalid from pg_index where indrelid = 't'::regclass") == [("t_code", False)]
