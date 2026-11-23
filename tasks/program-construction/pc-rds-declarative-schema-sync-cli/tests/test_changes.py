"""Planning and applying changes to existing databases: alterations, data, drift, drops and failures."""

from library_state import LIBRARY_ORDER, LIBRARY_PLAN, SHOP_V1_CATALOG
from schema_sync import EMPTY_DATABASE, assert_apply_plan, assert_catalog, assert_plan, assert_synced, errors, ok

SHOP_V1_PLAN = [
    """CREATE TYPE "public"."status" AS ENUM ('active', 'inactive')""",
    """CREATE TABLE "public"."users" ("id" bigint NOT NULL, "email" character varying(255) NOT NULL,
      "status" "public"."status" NOT NULL DEFAULT 'active', "nickname" text NULL, "age" integer NULL,
      PRIMARY KEY ("id"))""",
    """CREATE UNIQUE INDEX "users_email" ON "public"."users" ("email")""",
    """CREATE TABLE "public"."legacy" ("id" integer NOT NULL, "user_id" bigint NULL,
      CONSTRAINT "legacy_user" FOREIGN KEY ("user_id") REFERENCES "public"."users" ("id")
      ON UPDATE NO ACTION ON DELETE NO ACTION)""",
    """CREATE TABLE "public"."orders" ("id" serial NOT NULL, "user_id" bigint NOT NULL,
      "total" numeric(10,2) NOT NULL DEFAULT 0, "note" text NULL, PRIMARY KEY ("id"),
      CONSTRAINT "orders_user" FOREIGN KEY ("user_id") REFERENCES "public"."users" ("id")
      ON UPDATE NO ACTION ON DELETE CASCADE, CONSTRAINT "total_nonneg" CHECK (total >= (0)::numeric))""",
    """CREATE INDEX "orders_user_idx" ON "public"."orders" ("user_id")""",
]
SHOP_V1_ORDER = [(0, 1), (1, 2), (1, 3), (1, 4), (4, 5)]

SHOP_V2_PLAN = [
    """ALTER TYPE "public"."status" ADD VALUE 'pending' AFTER 'active'""",
    """ALTER TYPE "public"."status" ADD VALUE 'banned'""",
    """ALTER TABLE "public"."users" ALTER COLUMN "email" TYPE character varying(320), DROP COLUMN "nickname",
      ALTER COLUMN "age" TYPE bigint, ADD COLUMN "created_at" timestamptz NOT NULL DEFAULT now()""",
    """DROP INDEX "public"."orders_user_idx\"""",
    """ALTER TABLE "public"."orders" DROP CONSTRAINT "orders_user", DROP CONSTRAINT "total_nonneg",
      ADD CONSTRAINT "total_nonneg" CHECK (total >= (1)::numeric), ALTER COLUMN "total" TYPE numeric(12,2),
      ALTER COLUMN "total" SET DEFAULT 1, ALTER COLUMN "note" SET NOT NULL, ALTER COLUMN "note" SET DEFAULT '',
      ADD CONSTRAINT "orders_user" FOREIGN KEY ("user_id") REFERENCES "public"."users" ("id")
      ON UPDATE NO ACTION ON DELETE RESTRICT""",
    """CREATE INDEX "orders_user_total" ON "public"."orders" ("user_id", "total" DESC)""",
    """CREATE TABLE "public"."audit" ("id" bigserial NOT NULL, "order_id" integer NOT NULL, PRIMARY KEY ("id"),
      CONSTRAINT "audit_order" FOREIGN KEY ("order_id") REFERENCES "public"."orders" ("id")
      ON UPDATE NO ACTION ON DELETE SET NULL)""",
    """DROP TABLE "public"."legacy\"""",
]
# No statement here depends on another: every referenced object exists already.
SHOP_V2_ORDER = []

SHOP_V2_CATALOG = """
col|public|audit|id|bigint|int8|None|64|0|NO|nextval('audit_id_seq'::regclass)
col|public|audit|order_id|integer|int4|None|32|0|NO|None
col|public|orders|id|integer|int4|None|32|0|NO|nextval('orders_id_seq'::regclass)
col|public|orders|user_id|bigint|int8|None|64|0|NO|None
col|public|orders|total|numeric|numeric|None|12|2|NO|1
col|public|orders|note|text|text|None|None|None|NO|''::text
col|public|users|id|bigint|int8|None|64|0|NO|None
col|public|users|email|character varying|varchar|320|None|None|NO|None
col|public|users|status|USER-DEFINED|status|None|None|None|NO|'active'::status
col|public|users|age|bigint|int8|None|64|0|YES|None
col|public|users|created_at|timestamp with time zone|timestamptz|None|None|None|NO|now()
con|public|users|users_pkey|p|PRIMARY KEY (id)
con|public|orders|orders_pkey|p|PRIMARY KEY (id)
con|public|orders|orders_user|f|FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT
con|public|orders|total_nonneg|c|CHECK ((total >= (1)::numeric))
con|public|audit|audit_order|f|FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL
con|public|audit|audit_pkey|p|PRIMARY KEY (id)
idx|public|audit_pkey|CREATE UNIQUE INDEX audit_pkey ON public.audit USING btree (id)
idx|public|orders_pkey|CREATE UNIQUE INDEX orders_pkey ON public.orders USING btree (id)
idx|public|orders_user_total|CREATE INDEX orders_user_total ON public.orders USING btree (user_id, total DESC)
idx|public|users_email|CREATE UNIQUE INDEX users_email ON public.users USING btree (email)
idx|public|users_pkey|CREATE UNIQUE INDEX users_pkey ON public.users USING btree (id)
enum|public|status|active,pending,inactive,banned
schema|public|standard public schema
"""


def apply(ctx, target, dev, state, *extra):
    return ctx.run("schema", "apply", "-u", ctx.url(target), "--to", f"file://{state}", "--dev-url", ctx.url(dev),
                   *extra)


def shop_v1(ctx, rows=True):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("shop_v1.hcl")
    assert_apply_plan(ok(apply(ctx, target, dev, "shop_v1.hcl", "--auto-approve")), SHOP_V1_PLAN, SHOP_V1_ORDER)
    assert_catalog(ctx, target, SHOP_V1_CATALOG)
    if rows:
        ctx.sql(target, """INSERT INTO users (id, email, nickname, age) VALUES (1, 'a@x.io', 'al', 30), (2, 'b@x.io', NULL, NULL);
                           INSERT INTO orders (user_id, total, note) VALUES (1, 5.25, 'n1'), (2, 7, 'n2');
                           INSERT INTO legacy VALUES (1, 1)""")
    return target, dev


def rows(ctx, db):
    return {
        "users": ctx.sql(db, "SELECT id, email, status::text, age FROM users ORDER BY id"),
        "orders": ctx.sql(db, "SELECT id, user_id, total::text, note FROM orders ORDER BY id"),
    }


def test_alter_tables_and_enum_preserving_rows(ctx):
    target, dev = shop_v1(ctx)
    ctx.fixture("shop_v2.hcl")
    diff = ok(ctx.run("schema", "diff", "--from", ctx.url(target), "--to", "file://shop_v2.hcl", "--dev-url", ctx.url(dev)))
    assert_plan(diff, SHOP_V2_PLAN, SHOP_V2_ORDER)
    assert_catalog(ctx, target, SHOP_V1_CATALOG)  # diff never changes the database
    assert_apply_plan(ok(apply(ctx, target, dev, "shop_v2.hcl", "--auto-approve")), SHOP_V2_PLAN, SHOP_V2_ORDER)
    assert_catalog(ctx, target, SHOP_V2_CATALOG)
    assert rows(ctx, target) == {
        "users": [(1, "a@x.io", "active", 30), (2, "b@x.io", "active", None)],
        "orders": [(1, 1, "5.25", "n1"), (2, 2, "7.00", "n2")],
    }
    assert ctx.sql(target, "SELECT count(*) FROM users WHERE created_at IS NULL") == [(0,)]
    assert_synced(ok(apply(ctx, target, dev, "shop_v2.hcl", "--auto-approve")))
    assert_catalog(ctx, dev, EMPTY_DATABASE)


def test_failed_statement_rolls_back_the_whole_plan(ctx):
    target, dev = shop_v1(ctx)
    before = rows(ctx, target)
    # The new enum value is used as a default in the same transaction, which PostgreSQL rejects.
    ctx.fixture("shop_v2_unsafe.hcl")
    result = apply(ctx, target, dev, "shop_v2_unsafe.hcl", "--auto-approve")
    assert "unsafe use of new value" in errors(result)
    assert_catalog(ctx, target, SHOP_V1_CATALOG)
    assert rows(ctx, target) == before
    assert ctx.sql(target, "SELECT count(*) FROM legacy") == [(1,)]


def test_violating_rows_abort_the_change(ctx):
    target, dev = shop_v1(ctx, rows=False)
    ctx.sql(target, "INSERT INTO users (id, email) VALUES (1, 'a@x.io'); INSERT INTO orders (user_id, total) VALUES (1, 5.25)")
    ctx.fixture("shop_v2.hcl")
    result = apply(ctx, target, dev, "shop_v2.hcl", "--auto-approve")
    assert "contains null values" in errors(result)
    assert_apply_plan(result.stdout, SHOP_V2_PLAN, SHOP_V2_ORDER)
    assert_catalog(ctx, target, SHOP_V1_CATALOG)
    assert ctx.sql(target, "SELECT id, user_id, total::text, note FROM orders") == [(1, 1, "5.25", None)]


ITEMS_V2_PLAN = [
    """CREATE TYPE "public"."kind" AS ENUM ('a', 'b')""",
    """ALTER TABLE "public"."items" ADD COLUMN "k" "public"."kind" NOT NULL""",
]

ITEMS_V1_CATALOG = """
col|public|items|id|integer|int4|None|32|0|NO|None
con|public|items|items_pkey|p|PRIMARY KEY (id)
idx|public|items_pkey|CREATE UNIQUE INDEX items_pkey ON public.items USING btree (id)
schema|public|standard public schema
"""


def test_transaction_modes(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("items_v1.hcl")
    ctx.fixture("items_v2.hcl")
    ok(apply(ctx, target, dev, "items_v1.hcl", "--auto-approve"))
    ctx.sql(target, "INSERT INTO items VALUES (1), (2)")
    # Adding a NOT NULL column without default fails on existing rows.
    for mode in ([], ["--tx-mode", "file"]):
        result = apply(ctx, target, dev, "items_v2.hcl", "--auto-approve", *mode)
        assert "contains null values" in errors(result)
        assert_apply_plan(result.stdout, ITEMS_V2_PLAN, [(0, 1)])
        assert_catalog(ctx, target, ITEMS_V1_CATALOG)
    # Without a transaction, the statements before the failing one stay applied.
    result = apply(ctx, target, dev, "items_v2.hcl", "--auto-approve", "--tx-mode", "none")
    assert "contains null values" in errors(result)
    assert_catalog(ctx, target, ITEMS_V1_CATALOG + "enum|public|kind|a,b")
    assert_apply_plan(ok(apply(ctx, target, dev, "items_v2.hcl", "--dry-run")), ITEMS_V2_PLAN[1:])
    assert ctx.sql(target, "SELECT id FROM items ORDER BY id") == [(1,), (2,)]


def test_drift_is_planned_and_corrected(ctx):
    target, dev = shop_v1(ctx)
    ctx.sql(target, """ALTER TABLE users ADD COLUMN extra int; CREATE INDEX users_age ON users (age);
                       ALTER TABLE orders DROP CONSTRAINT total_nonneg; ALTER TABLE orders ALTER COLUMN note SET DEFAULT 'x';
                       ALTER TYPE status ADD VALUE 'zombie'; CREATE TABLE stray (id int)""")
    correction = [
        """ALTER TABLE "public"."orders" ADD CONSTRAINT "total_nonneg" CHECK (total >= (0)::numeric),
          ALTER COLUMN "note" DROP DEFAULT""",
        """DROP INDEX "public"."users_age\"""",
        """ALTER TABLE "public"."users" DROP COLUMN "extra\"""",
        """DROP TABLE "public"."stray\"""",
    ]
    diff = ok(ctx.run("schema", "diff", "--from", ctx.url(target), "--to", "file://shop_v1.hcl", "--dev-url", ctx.url(dev)))
    assert_plan(diff, correction)
    assert_apply_plan(ok(apply(ctx, target, dev, "shop_v1.hcl", "--auto-approve")), correction)
    # Enum values are never removed by a plan.
    assert_catalog(ctx, target, SHOP_V1_CATALOG.replace("active,inactive", "active,inactive,zombie"))
    assert rows(ctx, target)["orders"] == [(1, 1, "5.25", "n1"), (2, 2, "7.00", "n2")]
    assert_synced(ok(apply(ctx, target, dev, "shop_v1.hcl", "--auto-approve")))


def test_dropping_tables_respects_references(ctx):
    target, dev = shop_v1(ctx)
    ctx.write("empty.hcl", 'schema "public" {\n  comment = "standard public schema"\n}\n')
    plan = [
        'DROP TABLE "public"."legacy"',
        'DROP TABLE "public"."orders"',
        'DROP TABLE "public"."users"',
        'DROP TYPE "public"."status"',
    ]
    order = [(0, 2), (1, 2), (2, 3)]
    assert_apply_plan(ok(apply(ctx, target, dev, "empty.hcl", "--dry-run")), plan, order)
    assert_catalog(ctx, target, SHOP_V1_CATALOG)
    assert_apply_plan(ok(apply(ctx, target, dev, "empty.hcl", "--auto-approve")), plan, order)
    assert_catalog(ctx, target, EMPTY_DATABASE)


def test_diff_between_two_databases(ctx):
    full, empty = ctx.database(), ctx.database()
    dev = ctx.database()
    ctx.fixture("library.hcl")
    ok(ctx.run("schema", "apply", "-u", ctx.url(full), "--to", "file://library.hcl", "--dev-url", ctx.url(dev),
               "--auto-approve"))
    drops = ['DROP TABLE "public"."books"', 'DROP TABLE "public"."authors"', 'DROP TYPE "public"."mood"',
             'DROP TABLE "public"."a_independent"']
    assert_plan(ok(ctx.run("schema", "diff", "--from", ctx.url(full), "--to", ctx.url(empty))), drops, [(0, 1), (1, 2)])
    assert_plan(ok(ctx.run("schema", "diff", "--from", ctx.url(empty), "--to", ctx.url(full))), LIBRARY_PLAN, LIBRARY_ORDER)
    assert_catalog(ctx, empty, EMPTY_DATABASE)


def test_diff_between_desired_state_files(ctx):
    dev = ctx.database()
    ctx.fixture("library.hcl")
    ctx.fixture("library.hcl", "states/a.hcl")
    ctx.write("states/b.hcl", 'table "extra" {\n  schema = schema.public\n  column "k" {\n    type = text\n  }\n}\n')
    extra = ['CREATE TABLE "public"."extra" ("k" text NOT NULL)']
    assert_plan(ok(ctx.run("schema", "diff", "--from", "file://library.hcl", "--to", "file://states",
                           "--dev-url", ctx.url(dev))), extra)
    assert_plan(ok(ctx.run("schema", "diff", "--from", "file://library.hcl", "--to", "file://states/a.hcl",
                           "--to", "file://states/b.hcl", "--dev-url", ctx.url(dev))), extra)
    assert_synced(ok(ctx.run("schema", "diff", "--from", "file://states", "--to", "file://states/b.hcl",
                             "--to", "file://library.hcl", "--dev-url", ctx.url(dev))))
    assert_catalog(ctx, dev, EMPTY_DATABASE)


CONTACTS_V1_CATALOG = """
col|crm|contacts|id|integer|int4|None|32|0|NO|None
col|crm|contacts|region|text|text|None|None|None|NO|None
col|crm|contacts|email|text|text|None|None|None|NO|None
col|crm|contacts|phone|text|text|None|None|None|NO|None
con|crm|contacts|contacts_pkey|p|PRIMARY KEY (id)
idx|crm|contacts_lookup|CREATE INDEX contacts_lookup ON crm.contacts USING btree (email)
idx|crm|contacts_pkey|CREATE UNIQUE INDEX contacts_pkey ON crm.contacts USING btree (id)
schema|crm|customer data
schema|public|standard public schema
cmt|crm|contacts||people
cmt|crm|contacts|email|primary address
"""

CONTACTS_V2_CATALOG = """
col|crm|contacts|id|integer|int4|None|32|0|NO|None
col|crm|contacts|region|text|text|None|None|None|NO|None
col|crm|contacts|email|text|text|None|None|None|NO|None
col|crm|contacts|phone|text|text|None|None|None|YES|None
con|crm|contacts|contacts_pkey|p|PRIMARY KEY (id, region)
idx|crm|contacts_lookup|CREATE UNIQUE INDEX contacts_lookup ON crm.contacts USING btree (region, email)
idx|crm|contacts_pkey|CREATE UNIQUE INDEX contacts_pkey ON crm.contacts USING btree (id, region)
schema|crm|customer records
schema|public|standard public schema
cmt|crm|contacts|email|work address
"""


def test_comments_keys_and_redefined_indexes(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("contacts_v1.hcl")
    ctx.fixture("contacts_v2.hcl")
    ok(apply(ctx, target, dev, "contacts_v1.hcl", "--auto-approve"))
    assert_catalog(ctx, target, CONTACTS_V1_CATALOG)
    ctx.sql(target, "INSERT INTO crm.contacts VALUES (1, 'eu', 'a@x', '1'), (2, 'us', 'b@x', '2')")
    forward = [
        """COMMENT ON SCHEMA "crm" IS 'customer records'""",
        'DROP INDEX "crm"."contacts_lookup"',
        """ALTER TABLE "crm"."contacts" DROP CONSTRAINT "contacts_pkey", ALTER COLUMN "phone" DROP NOT NULL,
          ADD PRIMARY KEY ("id", "region")""",
        'CREATE UNIQUE INDEX "contacts_lookup" ON "crm"."contacts" ("region", "email")',
        """COMMENT ON TABLE "crm"."contacts" IS ''""",
        """COMMENT ON COLUMN "crm"."contacts"."email" IS 'work address'""",
    ]
    # An index that keeps its name but changes definition is dropped before it is recreated.
    diff = ok(ctx.run("schema", "diff", "--from", ctx.url(target), "--to", "file://contacts_v2.hcl",
                      "--dev-url", ctx.url(dev)))
    assert_plan(diff, forward, [(1, 3)])
    assert_apply_plan(ok(apply(ctx, target, dev, "contacts_v2.hcl", "--auto-approve")), forward, [(1, 3)])
    assert_catalog(ctx, target, CONTACTS_V2_CATALOG)
    assert ctx.sql(target, "SELECT * FROM crm.contacts ORDER BY id") == [(1, "eu", "a@x", "1"), (2, "us", "b@x", "2")]
    assert_synced(ok(apply(ctx, target, dev, "contacts_v2.hcl", "--auto-approve")))
    back = [
        """COMMENT ON SCHEMA "crm" IS 'customer data'""",
        'DROP INDEX "crm"."contacts_lookup"',
        """ALTER TABLE "crm"."contacts" DROP CONSTRAINT "contacts_pkey", ALTER COLUMN "phone" SET NOT NULL,
          ADD PRIMARY KEY ("id")""",
        'CREATE INDEX "contacts_lookup" ON "crm"."contacts" ("email")',
        """COMMENT ON TABLE "crm"."contacts" IS 'people'""",
        """COMMENT ON COLUMN "crm"."contacts"."email" IS 'primary address'""",
    ]
    out = ok(ctx.run("schema", "diff", "--from", ctx.url(target), "--to", "file://contacts_v1.hcl",
                     "--dev-url", ctx.url(dev), "--format", '{{ sql . "  " }}'))
    assert_plan(out, back, [(1, 3)])
    assert_catalog(ctx, target, CONTACTS_V2_CATALOG)
