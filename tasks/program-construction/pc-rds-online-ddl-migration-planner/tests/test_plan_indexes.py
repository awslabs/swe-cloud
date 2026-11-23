"""Plans for indexes and constraints: online builds, rename-swap replacement, NOT VALID + VALIDATE."""

from planlib import S, T20M, assert_plan, plan_change

BUILD = {"INDEX_BUILD"}
DROPPED = {"INDEX_DROPPED"}


def test_add_indexes_concurrently(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int PRIMARY KEY, a int, b text);", """
        CREATE TABLE t(id int PRIMARY KEY, a int, b text);
        CREATE INDEX t_a_idx ON t(a);
        CREATE UNIQUE INDEX t_b_uniq ON t(b) WHERE b IS NOT NULL;
        CREATE INDEX t_lower_b ON t (lower(b) DESC);""")
    assert_plan(actual, [
        S("CREATE INDEX CONCURRENTLY t_a_idx ON public.t USING btree (a)", T20M, hazards=BUILD),
        S("CREATE UNIQUE INDEX CONCURRENTLY t_b_uniq ON public.t USING btree (b) WHERE (b IS NOT NULL)", T20M, hazards=BUILD),
        S("CREATE INDEX CONCURRENTLY t_lower_b ON public.t USING btree (lower(b) DESC)", T20M, hazards=BUILD),
    ])


def test_drop_index_concurrently(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int PRIMARY KEY, a int); CREATE INDEX t_a_idx ON t(a);",
                         "CREATE TABLE t(id int PRIMARY KEY, a int);")
    assert_plan(actual, [S('DROP INDEX CONCURRENTLY "public"."t_a_idx"', T20M, hazards=DROPPED)])


def test_changed_indexes_are_replaced_by_rename_swap(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE t(id int PRIMARY KEY, a int, b int); CREATE INDEX t_ab ON t(a); CREATE INDEX t_h ON t(b);",
                         "CREATE TABLE t(id int PRIMARY KEY, a int, b int); CREATE INDEX t_ab ON t(a, b); CREATE INDEX t_h ON t USING hash (b);")
    expected = [
        S('ALTER INDEX "public"."t_ab" RENAME TO "pgschemadiff_tmpidx_t_ab_{x}"'),
        S('ALTER INDEX "public"."t_h" RENAME TO "pgschemadiff_tmpidx_t_h_{y}"'),
        S("CREATE INDEX CONCURRENTLY t_ab ON public.t USING btree (a, b)", T20M, hazards=BUILD),
        S("CREATE INDEX CONCURRENTLY t_h ON public.t USING hash (b)", T20M, hazards=BUILD),
        S('DROP INDEX CONCURRENTLY "public"."pgschemadiff_tmpidx_t_ab_{x}"', T20M, hazards=DROPPED),
        S('DROP INDEX CONCURRENTLY "public"."pgschemadiff_tmpidx_t_h_{y}"', T20M, hazards=DROPPED),
    ]
    assert_plan(actual, expected, order=[(0, 2, 4), (1, 3, 5)])


def test_no_concurrent_index_ops(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int PRIMARY KEY, a int); CREATE INDEX t_old ON t(id, a);",
                         "CREATE TABLE t(id int PRIMARY KEY, a int); CREATE INDEX t_a_idx ON t(a); CREATE INDEX t_old ON t(a, id);",
                         args=["--no-concurrent-index-ops"])
    expected = [
        S('ALTER INDEX "public"."t_old" RENAME TO "pgschemadiff_tmpidx_t_old_{x}"'),
        S("CREATE INDEX t_a_idx ON public.t USING btree (a)", hazards={"ACQUIRES_SHARE_LOCK"}),
        S("CREATE INDEX t_old ON public.t USING btree (a, id)", hazards={"ACQUIRES_SHARE_LOCK"}),
        S('DROP INDEX "public"."pgschemadiff_tmpidx_t_old_{x}"', hazards={"ACQUIRES_ACCESS_EXCLUSIVE_LOCK", "INDEX_DROPPED"}),
    ]
    assert_plan(actual, expected, order=[(0, 2, 3)])


def test_index_on_new_column(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int);",
                         "CREATE TABLE t(id int, email text NOT NULL DEFAULT ''); CREATE UNIQUE INDEX t_email ON t(email);")
    expected = [
        S('''ALTER TABLE "public"."t" ADD COLUMN "email" text COLLATE "pg_catalog"."default" DEFAULT ''::text NOT NULL'''),
        S("CREATE UNIQUE INDEX CONCURRENTLY t_email ON public.t USING btree (email)", T20M, hazards=BUILD),
    ]
    assert_plan(actual, expected, order=[(0, 1)])


def test_drop_index_before_its_columns(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE t(id int, a int, b int); CREATE INDEX t_ab ON t(a, b); CREATE INDEX t_id ON t(id);",
                         "CREATE TABLE t(id int); CREATE INDEX t_id ON t(id);")
    expected = [
        S('DROP INDEX CONCURRENTLY "public"."t_ab"', T20M, hazards=DROPPED),
        S('ALTER TABLE "public"."t" DROP COLUMN "a"', hazards={"DELETES_DATA"}),
        S('ALTER TABLE "public"."t" DROP COLUMN "b"', hazards={"DELETES_DATA"}),
    ]
    assert_plan(actual, expected, order=[(0, 1), (0, 2)])


def test_add_primary_key_and_unique_constraint_to_existing_table(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int, code text); INSERT INTO t VALUES (1, 'a');",
                         "CREATE TABLE t(id int PRIMARY KEY, code text UNIQUE);")
    expected = [
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "pgschemadiff_tmpnn_{n}" CHECK("id" IS NOT NULL) NOT VALID'),
        S('ALTER TABLE "public"."t" VALIDATE CONSTRAINT "pgschemadiff_tmpnn_{n}"'),
        S('ALTER TABLE "public"."t" ALTER COLUMN "id" SET NOT NULL'),
        S('ALTER TABLE "public"."t" DROP CONSTRAINT "pgschemadiff_tmpnn_{n}"'),
        S("CREATE UNIQUE INDEX CONCURRENTLY t_code_key ON public.t USING btree (code)", T20M, hazards=BUILD),
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "t_code_key" UNIQUE USING INDEX "t_code_key"'),
        S("CREATE UNIQUE INDEX CONCURRENTLY t_pkey ON public.t USING btree (id)", T20M, hazards=BUILD),
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "t_pkey" PRIMARY KEY USING INDEX "t_pkey"'),
    ]
    assert_plan(actual, expected, order=[(0, 1, 2, 3), (2, 7), (4, 5), (6, 7)])


def test_constraints_reuse_existing_unique_indexes(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE t(id int NOT NULL, c int); CREATE UNIQUE INDEX t_pkey ON t(id); CREATE UNIQUE INDEX other_idx ON t(c);",
                         "CREATE TABLE t(id int PRIMARY KEY, c int); CREATE UNIQUE INDEX other_idx ON t(c);"
                         " ALTER TABLE t ADD CONSTRAINT t_c_key UNIQUE USING INDEX other_idx;")
    expected = [
        S("CREATE UNIQUE INDEX CONCURRENTLY t_c_key ON public.t USING btree (c)", T20M, hazards=BUILD),
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "t_c_key" UNIQUE USING INDEX "t_c_key"'),
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "t_pkey" PRIMARY KEY USING INDEX "t_pkey"'),
        S('DROP INDEX CONCURRENTLY "public"."other_idx"', T20M, hazards=DROPPED),
    ]
    assert_plan(actual, expected, order=[(0, 1)])


def test_switch_primary_key(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int PRIMARY KEY, alt int NOT NULL);",
                         "CREATE TABLE t(id int, alt int PRIMARY KEY);")
    expected = [
        S('ALTER TABLE "public"."t" DROP CONSTRAINT "t_pkey"', hazards={"ACQUIRES_ACCESS_EXCLUSIVE_LOCK", "INDEX_DROPPED"}),
        S('ALTER TABLE "public"."t" ALTER COLUMN "id" DROP NOT NULL'),
        S("CREATE UNIQUE INDEX CONCURRENTLY t_pkey ON public.t USING btree (alt)", T20M, hazards=BUILD),
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "t_pkey" PRIMARY KEY USING INDEX "t_pkey"'),
    ]
    assert_plan(actual, expected, order=[(0, 1), (0, 2, 3)])


def test_drop_primary_key_and_unique_constraint(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE t(id int PRIMARY KEY, code text UNIQUE);",
                         "CREATE TABLE t(id int, code text);")
    expected = [
        S('ALTER TABLE "public"."t" DROP CONSTRAINT "t_code_key"', hazards={"ACQUIRES_ACCESS_EXCLUSIVE_LOCK", "INDEX_DROPPED"}),
        S('ALTER TABLE "public"."t" DROP CONSTRAINT "t_pkey"', hazards={"ACQUIRES_ACCESS_EXCLUSIVE_LOCK", "INDEX_DROPPED"}),
        S('ALTER TABLE "public"."t" ALTER COLUMN "id" DROP NOT NULL'),
    ]
    assert_plan(actual, expected, order=[(1, 2)])


def test_check_constraints_added_altered_and_dropped(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir,
                         "CREATE TABLE t(id int PRIMARY KEY, a int CONSTRAINT a_pos CHECK (a > 0), b int CONSTRAINT b_small CHECK (b < 10));",
                         "CREATE TABLE t(id int PRIMARY KEY, a int CONSTRAINT a_pos CHECK (a > 1), b int, c int CONSTRAINT c_chk CHECK (c <> 0));")
    expected = [
        S('ALTER TABLE "public"."t" ADD COLUMN "c" integer'),
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "c_chk" CHECK((c <> 0)) NOT VALID'),
        S('ALTER TABLE "public"."t" VALIDATE CONSTRAINT "c_chk"'),
        S('ALTER TABLE "public"."t" DROP CONSTRAINT "a_pos"'),
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "a_pos" CHECK((a > 1)) NOT VALID'),
        S('ALTER TABLE "public"."t" VALIDATE CONSTRAINT "a_pos"'),
        S('ALTER TABLE "public"."t" DROP CONSTRAINT "b_small"'),
    ]
    assert_plan(actual, expected, order=[(0, 1, 2), (3, 4, 5)])


def test_constraint_validation_state_changes(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, """
        CREATE TABLE t(id int, a int, b int CONSTRAINT b_nn CHECK (b IS NOT NULL), c int CONSTRAINT c_pos CHECK (c > 0));
        ALTER TABLE t ADD CONSTRAINT a_pos CHECK (a > 0) NOT VALID;""", """
        CREATE TABLE t(id int, a int CONSTRAINT a_pos CHECK (a > 0), b int NOT NULL CONSTRAINT b_nn CHECK (b IS NOT NULL), c int);
        ALTER TABLE t ADD CONSTRAINT c_pos CHECK (c > 0) NOT VALID;""")
    expected = [
        S('ALTER TABLE "public"."t" VALIDATE CONSTRAINT "a_pos"'),
        S('ALTER TABLE "public"."t" ALTER COLUMN "b" SET NOT NULL'),
        S('ALTER TABLE "public"."t" DROP CONSTRAINT "c_pos"'),
        S('ALTER TABLE "public"."t" ADD CONSTRAINT "c_pos" CHECK((c > 0)) NOT VALID'),
    ]
    assert_plan(actual, expected, order=[(2, 3)])


def test_foreign_keys_added_dropped_and_altered(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, """
        CREATE TABLE p(id int PRIMARY KEY);
        CREATE TABLE c(id int PRIMARY KEY, pid int);
        CREATE TABLE c2(id int, pid int CONSTRAINT c2_fk REFERENCES p(id));
        CREATE TABLE c3(pid int CONSTRAINT c3_fk REFERENCES p(id));
        CREATE TABLE c4(pid int);
        ALTER TABLE c4 ADD CONSTRAINT c4_fk FOREIGN KEY (pid) REFERENCES p(id) NOT VALID;""", """
        CREATE TABLE p(id int PRIMARY KEY);
        CREATE TABLE c(id int PRIMARY KEY, pid int REFERENCES p(id) ON DELETE CASCADE);
        CREATE TABLE c2(id int, pid int);
        CREATE TABLE c3(pid int CONSTRAINT c3_fk REFERENCES p(id) ON DELETE SET NULL);
        CREATE TABLE c4(pid int CONSTRAINT c4_fk REFERENCES p(id));""")
    expected = [
        S('ALTER TABLE "public"."c2" DROP CONSTRAINT "c2_fk"'),
        S('ALTER TABLE "public"."c" ADD CONSTRAINT "c_pid_fkey" FOREIGN KEY (pid) REFERENCES p(id) ON DELETE CASCADE NOT VALID'),
        S('ALTER TABLE "public"."c" VALIDATE CONSTRAINT "c_pid_fkey"'),
        S('ALTER TABLE "public"."c3" DROP CONSTRAINT "c3_fk"'),
        S('ALTER TABLE "public"."c3" ADD CONSTRAINT "c3_fk" FOREIGN KEY (pid) REFERENCES p(id) ON DELETE SET NULL NOT VALID'),
        S('ALTER TABLE "public"."c3" VALIDATE CONSTRAINT "c3_fk"'),
        S('ALTER TABLE "public"."c4" VALIDATE CONSTRAINT "c4_fk"'),
    ]
    assert_plan(actual, expected, order=[(1, 2), (3, 4, 5)])


def test_foreign_key_to_new_table_follows_its_primary_key(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE c(id int, pid int);",
                         "CREATE TABLE p(id int PRIMARY KEY, name text); CREATE TABLE c(id int, pid int REFERENCES p(id));")
    expected = [
        S('CREATE TABLE "public"."p" ("id" integer NOT NULL, "name" text COLLATE "pg_catalog"."default")'),
        S("CREATE UNIQUE INDEX CONCURRENTLY p_pkey ON public.p USING btree (id)", T20M),
        S('ALTER TABLE "public"."p" ADD CONSTRAINT "p_pkey" PRIMARY KEY USING INDEX "p_pkey"'),
        S('ALTER TABLE "public"."c" ADD CONSTRAINT "c_pid_fkey" FOREIGN KEY (pid) REFERENCES p(id) NOT VALID'),
        S('ALTER TABLE "public"."c" VALIDATE CONSTRAINT "c_pid_fkey"'),
    ]
    assert_plan(actual, expected, order=[(0, 1, 2, 3, 4)])


def test_drop_referenced_table_after_its_foreign_key(server, db, schema_dir):
    actual = plan_change(server, db, schema_dir, "CREATE TABLE p(id int PRIMARY KEY); CREATE TABLE c(id int, pid int REFERENCES p(id));",
                         "CREATE TABLE c(id int, pid int);")
    expected = [
        S('ALTER TABLE "public"."c" DROP CONSTRAINT "c_pid_fkey"'),
        S('DROP TABLE "public"."p"', T20M, hazards={"DELETES_DATA"}),
    ]
    assert_plan(actual, expected, order=[(0, 1)])
