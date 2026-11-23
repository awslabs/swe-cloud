"""Applying desired states to empty databases: plans, catalog effects and dev-database normalization."""

from library_state import LIBRARY_CATALOG, LIBRARY_ORDER, LIBRARY_PLAN, SHOP_V1_CATALOG
from schema_sync import EMPTY_DATABASE, assert_apply_plan, assert_catalog, assert_plan, assert_synced, errors, ok

def unqualified(statement):
    return statement.replace('"public".', "")


def qualified(statement):
    return statement.replace('TABLE "', 'TABLE "public"."')


def test_apply_creates_every_object_kind(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("library.hcl")
    out = ok(ctx.run("schema", "apply", "-u", ctx.url(target), "--to", "file://library.hcl",
                     "--dev-url", ctx.url(dev), "--auto-approve"))
    assert_apply_plan(out, LIBRARY_PLAN, LIBRARY_ORDER)
    assert_catalog(ctx, target, LIBRARY_CATALOG)
    # The dev database is scratch space only and is left clean.
    assert_catalog(ctx, dev, EMPTY_DATABASE)


def test_dry_run_prints_plan_without_changes(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("library.hcl")
    out = ok(ctx.run("schema", "apply", "-u", ctx.url(target), "--to", "file://library.hcl",
                     "--dev-url", ctx.url(dev), "--dry-run"))
    assert_apply_plan(out, LIBRARY_PLAN, LIBRARY_ORDER)
    assert_catalog(ctx, target, EMPTY_DATABASE)
    assert_catalog(ctx, dev, EMPTY_DATABASE)


def test_reapplying_the_same_state_is_synced(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("library.hcl")
    args = ["-u", ctx.url(target), "--to", "file://library.hcl", "--dev-url", ctx.url(dev)]
    ok(ctx.run("schema", "apply", *args, "--auto-approve"))
    assert_synced(ok(ctx.run("schema", "apply", *args, "--auto-approve")))
    assert_synced(ok(ctx.run("schema", "apply", *args, "--dry-run")))
    assert_synced(ok(ctx.run("schema", "diff", "--from", ctx.url(target), "--to", "file://library.hcl",
                             "--dev-url", ctx.url(dev))))
    assert_catalog(ctx, target, LIBRARY_CATALOG)


def test_schema_scoped_urls_use_unqualified_names(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("library.hcl")
    args = ["-u", ctx.url(target, "public"), "--to", "file://library.hcl", "--dev-url", ctx.url(dev, "public")]
    out = ok(ctx.run("schema", "apply", *args, "--auto-approve"))
    assert_apply_plan(out, [unqualified(statement) for statement in LIBRARY_PLAN], LIBRARY_ORDER)
    assert_catalog(ctx, target, LIBRARY_CATALOG)
    assert_synced(ok(ctx.run("schema", "apply", *args, "--auto-approve")))


BILLING_PLAN = [
    """CREATE SCHEMA "billing\"""",
    """COMMENT ON SCHEMA "billing" IS 'money'""",
    """CREATE TABLE "public"."customers" ("id" integer NOT NULL, "name" text NOT NULL, PRIMARY KEY ("id"))""",
    """CREATE TYPE "billing"."currency" AS ENUM ('usd', 'eur')""",
    """CREATE TABLE "billing"."invoices" ("id" bigint NOT NULL, "customer_id" integer NOT NULL,
      "cur" "billing"."currency" NOT NULL DEFAULT 'usd', "amount" numeric(9,3) NOT NULL DEFAULT 0.5,
      PRIMARY KEY ("id", "customer_id"), CONSTRAINT "inv_customer" FOREIGN KEY ("customer_id")
      REFERENCES "public"."customers" ("id") ON UPDATE CASCADE ON DELETE SET DEFAULT)""",
    """CREATE INDEX "inv_amount_brin" ON "billing"."invoices" USING brin ("amount")""",
]
BILLING_ORDER = [(0, 1), (0, 3), (0, 4), (3, 4), (2, 4), (4, 5)]

BILLING_CATALOG = """
col|billing|invoices|id|bigint|int8|None|64|0|NO|None
col|billing|invoices|customer_id|integer|int4|None|32|0|NO|None
col|billing|invoices|cur|USER-DEFINED|currency|None|None|None|NO|'usd'::billing.currency
col|billing|invoices|amount|numeric|numeric|None|9|3|NO|0.5
col|public|customers|id|integer|int4|None|32|0|NO|None
col|public|customers|name|text|text|None|None|None|NO|None
con|public|customers|customers_pkey|p|PRIMARY KEY (id)
con|billing|invoices|inv_customer|f|FOREIGN KEY (customer_id) REFERENCES customers(id) ON UPDATE CASCADE ON DELETE SET DEFAULT
con|billing|invoices|invoices_pkey|p|PRIMARY KEY (id, customer_id)
idx|billing|inv_amount_brin|CREATE INDEX inv_amount_brin ON billing.invoices USING brin (amount)
idx|billing|invoices_pkey|CREATE UNIQUE INDEX invoices_pkey ON billing.invoices USING btree (id, customer_id)
idx|public|customers_pkey|CREATE UNIQUE INDEX customers_pkey ON public.customers USING btree (id)
enum|billing|currency|usd,eur
schema|billing|money
schema|public|standard public schema
"""


def test_multiple_schemas_with_cross_schema_reference(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("billing.hcl")
    args = ["-u", ctx.url(target), "--to", "file://billing.hcl", "--dev-url", ctx.url(dev)]
    assert_apply_plan(ok(ctx.run("schema", "apply", *args, "--dry-run")), BILLING_PLAN, BILLING_ORDER)
    assert_catalog(ctx, target, EMPTY_DATABASE)
    assert_apply_plan(ok(ctx.run("schema", "apply", *args, "--auto-approve")), BILLING_PLAN, BILLING_ORDER)
    assert_catalog(ctx, target, BILLING_CATALOG)
    assert_catalog(ctx, dev, EMPTY_DATABASE)


def test_requested_schemas_must_cover_the_desired_state(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("billing.hcl")
    base = ["schema", "apply", "-u", ctx.url(target), "--to", "file://billing.hcl", "--dev-url", ctx.url(dev)]
    result = ctx.run(*base, "--schema", "public", "--dry-run")
    assert result.returncode == 1 and '"billing"' in result.stderr, result
    assert_catalog(ctx, target, EMPTY_DATABASE)
    # --exclude drops matching objects from both sides; the rest of the state is applied.
    out = ok(ctx.run(*base, "--exclude", "billing.invoices", "--auto-approve"))
    assert_apply_plan(out, BILLING_PLAN[:4], [(0, 1), (0, 3)])
    assert_catalog(ctx, target, """
        col|public|customers|id|integer|int4|None|32|0|NO|None
        col|public|customers|name|text|text|None|None|None|NO|None
        con|public|customers|customers_pkey|p|PRIMARY KEY (id)
        idx|public|customers_pkey|CREATE UNIQUE INDEX customers_pkey ON public.customers USING btree (id)
        enum|billing|currency|usd,eur
        schema|billing|money
        schema|public|standard public schema
    """)


def test_desired_state_without_public_drops_it(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("shop_v1.hcl")
    ok(ctx.run("schema", "apply", "-u", ctx.url(target), "--to", "file://shop_v1.hcl", "--dev-url", ctx.url(dev),
               "--auto-approve"))
    ctx.write("other.hcl", 'schema "other" {}\n')
    args = ["-u", ctx.url(target), "--to", "file://other.hcl", "--dev-url", ctx.url(dev)]
    plan = ['DROP SCHEMA "public" CASCADE', 'CREATE SCHEMA "other"']
    assert_apply_plan(ok(ctx.run("schema", "apply", *args, "--dry-run")), plan)
    assert_catalog(ctx, target, SHOP_V1_CATALOG)
    assert_apply_plan(ok(ctx.run("schema", "apply", *args, "--auto-approve")), plan)
    assert_catalog(ctx, target, "schema|other|None")


NO_DEV_PLAN = [
    """CREATE TYPE "public"."mood" AS ENUM ('happy', 'sad')""",
    """CREATE TABLE "public"."authors" (
      "id" bigserial NOT NULL, "name" character varying(100) NOT NULL, "bio" text NULL,
      "active" boolean NOT NULL DEFAULT true, "score" numeric(8,2) NOT NULL DEFAULT 1.5,
      "created" timestamptz NOT NULL DEFAULT now(), "tags" text[] NULL, "mood" "public"."mood" NULL,
      "code" character(2) NOT NULL DEFAULT 'xx', "ratio" double precision NULL, "uid" uuid NULL,
      "meta" jsonb NOT NULL DEFAULT '{}'::jsonb, PRIMARY KEY ("id"),
      CONSTRAINT "score_range" CHECK (score >= 0 AND score <= 100))""",
    """CREATE INDEX "authors_name_lower" ON "public"."authors" ((lower((name)::text)))""",
    """CREATE INDEX "authors_created_desc" ON "public"."authors" ("created" DESC, "id")""",
    """CREATE UNIQUE INDEX "authors_code_partial" ON "public"."authors" ("code") WHERE active""",
    """CREATE INDEX "authors_tags_gin" ON "public"."authors" USING GIN ("tags")""",
    """CREATE INDEX "authors_uid_hash" ON "public"."authors" USING HASH ("uid")""",
    """COMMENT ON TABLE "public"."authors" IS 'people who write'""",
    """COMMENT ON COLUMN "public"."authors"."name" IS 'display name'""",
    """CREATE TABLE "public"."a_independent" ("x" smallint NOT NULL)""",
    """CREATE TABLE "public"."books" ("id" integer NOT NULL, "author_id" bigint NULL, "title" text NOT NULL,
      PRIMARY KEY ("id"), CONSTRAINT "books_author_fk" FOREIGN KEY ("author_id") REFERENCES "public"."authors" ("id")
      ON UPDATE RESTRICT ON DELETE SET NULL)""",
]


def test_apply_without_dev_database_compares_written_forms(ctx):
    target = ctx.database()
    ctx.fixture("library.hcl")
    args = ["schema", "apply", "-u", ctx.url(target), "--to", "file://library.hcl", "--auto-approve"]
    out = ok(ctx.run(*args))
    assert_apply_plan(out, NO_DEV_PLAN, [(0, 1)] + [(1, n) for n in range(2, 9)] + [(1, 10)])
    assert_catalog(ctx, target, LIBRARY_CATALOG)
    # Without normalization the written CHECK differs from the stored form, so it is planned again each time.
    recheck = ['''ALTER TABLE "public"."authors" DROP CONSTRAINT "score_range",
                  ADD CONSTRAINT "score_range" CHECK (score >= 0 AND score <= 100)''']
    assert_apply_plan(ok(ctx.run(*args)), recheck)
    assert_catalog(ctx, target, LIBRARY_CATALOG)
    assert_apply_plan(ok(ctx.run(*args)), recheck)


BILLING_SCOPED_CATALOG = BILLING_CATALOG + """
col|public|customers|email|text|text|None|None|None|YES|None
col|public|items|id|integer|int4|None|32|0|NO|None
con|public|items|items_pkey|p|PRIMARY KEY (id)
idx|public|items_pkey|CREATE UNIQUE INDEX items_pkey ON public.items USING btree (id)
"""


def scoped_billing(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("billing.hcl")
    ctx.fixture("public_only.hcl")
    ctx.fixture("items_v1.hcl")
    ok(ctx.run("schema", "apply", "-u", ctx.url(target), "--to", "file://billing.hcl", "--dev-url", ctx.url(dev),
               "--auto-approve"))
    ctx.sql(target, "INSERT INTO customers VALUES (1, 'ann'); INSERT INTO billing.invoices VALUES (10, 1, 'eur', 2.5)")
    return target, dev


def billing_rows(ctx, db):
    return (ctx.sql(db, "SELECT id, name FROM customers"),
            ctx.sql(db, "SELECT id, customer_id, cur::text, amount::text FROM billing.invoices"))


SCOPED_ROWS = ([(1, "ann")], [(10, 1, "eur", "2.500")])


def test_scoped_url_manages_only_its_schema(ctx):
    target, dev = scoped_billing(ctx)
    scoped = ["-u", ctx.url(target, "public"), "--dev-url", ctx.url(dev, "public")]
    plan = ['ALTER TABLE "customers" ADD COLUMN "email" text NULL',
            'CREATE TABLE "items" ("id" integer NOT NULL, PRIMARY KEY ("id"))']
    # The whole-database view of the same state would also drop the other schema.
    assert_plan(ok(ctx.run("schema", "diff", "--from", ctx.url(target), "--to", "file://public_only.hcl",
                           "--dev-url", ctx.url(dev))),
                ['DROP SCHEMA "billing" CASCADE', *[qualified(statement) for statement in plan]])
    assert_apply_plan(ok(ctx.run("schema", "apply", *scoped, "--to", "file://public_only.hcl", "--auto-approve")), plan)
    assert_catalog(ctx, target, BILLING_SCOPED_CATALOG)
    assert billing_rows(ctx, target) == SCOPED_ROWS
    assert_synced(ok(ctx.run("schema", "apply", *scoped, "--to", "file://public_only.hcl", "--auto-approve")))
    # Dropping a table that the unmanaged schema references fails, and nothing is applied.
    result = ctx.run("schema", "apply", *scoped, "--to", "file://items_v1.hcl", "--auto-approve")
    assert "depend on it" in errors(result)
    assert_apply_plan(result.stdout, ['DROP TABLE "customers"'])
    assert_catalog(ctx, target, BILLING_SCOPED_CATALOG)
    assert billing_rows(ctx, target) == SCOPED_ROWS
    assert_catalog(ctx, dev, EMPTY_DATABASE)


def test_schema_flag_restricts_a_database_url(ctx):
    target, dev = scoped_billing(ctx)
    args = ["-u", ctx.url(target), "--dev-url", ctx.url(dev), "--schema", "public"]
    plan = ['ALTER TABLE "public"."customers" ADD COLUMN "email" text NULL',
            'CREATE TABLE "public"."items" ("id" integer NOT NULL, PRIMARY KEY ("id"))']
    assert_apply_plan(ok(ctx.run("schema", "apply", *args, "--to", "file://public_only.hcl", "--dry-run")), plan)
    assert_catalog(ctx, target, BILLING_CATALOG)
    assert_apply_plan(ok(ctx.run("schema", "apply", *args, "--to", "file://public_only.hcl", "--auto-approve")), plan)
    assert_catalog(ctx, target, BILLING_SCOPED_CATALOG)
    assert billing_rows(ctx, target) == SCOPED_ROWS
    assert_synced(ok(ctx.run("schema", "diff", "--from", ctx.url(target), "--to", "file://public_only.hcl",
                             "--dev-url", ctx.url(dev), "--schema", "public")))
