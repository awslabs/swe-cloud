"""Project-file environments whose credentials come from Secrets Manager and Parameter Store."""

import json
import re

import boto3

from schema_sync import (
    EMPTY_DATABASE,
    assert_apply_plan,
    assert_catalog,
    assert_hcl,
    assert_plan,
    assert_synced,
    FIXTURES,
    errors,
    expected,
    ok,
)

ITEMS_CATALOG = """
col|public|items|id|integer|int4|None|32|0|NO|None
con|public|items|items_pkey|p|PRIMARY KEY (id)
idx|public|items_pkey|CREATE UNIQUE INDEX items_pkey ON public.items USING btree (id)
schema|public|standard public schema
"""
CREATE_ITEMS = ['CREATE TABLE "public"."items" ("id" integer NOT NULL, PRIMARY KEY ("id"))']
ADD_NAME = ['ALTER TABLE "public"."items" ADD COLUMN "name" text NULL']
NAMED_CATALOG = ITEMS_CATALOG + "col|public|items|name|text|text|None|None|None|YES|None"


def render(ctx, template, **values):
    server = ctx.server
    values = {"USER": server.user, "HOST": server.host, "PORT": str(server.port), **values}
    for key, value in values.items():
        template = template.replace(f"@{key}@", value)
    assert not re.search(r"@[A-Z]+@", template), template
    ctx.write("atlas.hcl", template)


def put_secret(name, value):
    boto3.client("secretsmanager").put_secret_value(SecretId=name, SecretString=value)


SM_PROJECT = """
variable "dbname" {
  type    = string
  default = "@TARGET@"
}

data "runtimevar" "pass" {
  url = "awssecretsmanager://@SECRET@?region=us-east-1"
}

data "runtimevar" "devpass" {
  url = "awsparamstore://@PARAM@?region=us-east-1"
}

data "runtimevar" "devname" {
  url = "awsparamstore://@DEVNAME@?region=us-east-1"
}

env "rds" {
  url = "postgres://@USER@:${urlescape(data.runtimevar.pass)}@@HOST@:@PORT@/${var.dbname}?sslmode=disable"
  dev = "postgres://@USER@:${urlescape(data.runtimevar.devpass)}@@HOST@:@PORT@/${data.runtimevar.devname}?sslmode=disable"
  src = "file://items_v1.hcl"
}
"""


def sm_project(ctx):
    target, dev = ctx.database(), ctx.database()
    secret = ctx.secret(ctx.server.password)
    param = ctx.parameter(ctx.server.password)  # "/sync/..." gives awsparamstore:///sync/...
    devname = ctx.parameter(dev, kind="String", prefix="")
    render(ctx, SM_PROJECT, TARGET=target, SECRET=secret, PARAM=param, DEVNAME=devname)
    ctx.fixture("items_v1.hcl")
    ctx.fixture("items_named.hcl")
    return target, dev, secret, param


def test_env_takes_credentials_from_secrets_manager_and_parameter_store(ctx):
    target, dev, _, _ = sm_project(ctx)
    assert_apply_plan(ok(ctx.run("schema", "apply", "--env", "rds", "--dry-run")), CREATE_ITEMS)
    assert_catalog(ctx, target, EMPTY_DATABASE)
    assert_apply_plan(ok(ctx.run("schema", "apply", "--env", "rds", "--auto-approve")), CREATE_ITEMS)
    assert_catalog(ctx, target, ITEMS_CATALOG)
    assert_catalog(ctx, dev, EMPTY_DATABASE)
    assert_synced(ok(ctx.run("schema", "diff", "--env", "rds", "--from", "env://url", "--to", "env://src")))
    assert_plan(ok(ctx.run("schema", "diff", "--env", "rds", "--from", "env://url", "--to", "file://items_named.hcl")),
                ADD_NAME)
    assert_hcl(ok(ctx.run("schema", "inspect", "--env", "rds")), expected("items.inspect.hcl"))
    # --var overrides the variable's default, so the environment now points at the dev database.
    assert_hcl(ok(ctx.run("schema", "inspect", "--env", "rds", "--var", f"dbname={dev}")),
               'schema "public" {\n  comment = "standard public schema"\n}\n')


def test_rotated_secret_takes_effect_on_the_next_run(ctx):
    target, _, secret, _ = sm_project(ctx)
    ok(ctx.run("schema", "apply", "--env", "rds", "--auto-approve"))
    put_secret(secret, "rotated-wrong-password")
    for args in (["schema", "apply", "--env", "rds", "--to", "file://items_named.hcl", "--auto-approve"],
                 ["schema", "inspect", "--env", "rds"],
                 ["schema", "diff", "--env", "rds", "--from", "env://url", "--to", "file://items_named.hcl"]):
        result = ctx.run(*args)
        assert "password authentication failed" in errors(result)
        assert ";" not in result.stdout
    assert_catalog(ctx, target, ITEMS_CATALOG)
    put_secret(secret, ctx.server.password)
    out = ok(ctx.run("schema", "apply", "--env", "rds", "--to", "file://items_named.hcl", "--auto-approve"))
    assert_apply_plan(out, ADD_NAME)
    assert_catalog(ctx, target, NAMED_CATALOG)


def test_wrong_dev_password_blocks_planning_but_not_inspection(ctx):
    target, _, _, param = sm_project(ctx)
    ok(ctx.run("schema", "apply", "--env", "rds", "--auto-approve"))
    boto3.client("ssm").put_parameter(Name=param, Value="bad", Type="SecureString", Overwrite=True)
    result = ctx.run("schema", "apply", "--env", "rds", "--to", "file://items_named.hcl", "--auto-approve")
    assert "password authentication failed" in errors(result)
    assert_catalog(ctx, target, ITEMS_CATALOG)
    assert_hcl(ok(ctx.run("schema", "inspect", "--env", "rds")), expected("items.inspect.hcl"))


JSON_PROJECT = """
data "runtimevar" "creds" {
  url = "awssecretsmanager://@SECRET@?region=us-east-1"
}

locals {
  c = jsondecode(data.runtimevar.creds)
}

env "json" {
  url = "postgres://${local.c.username}:${urlescape(local.c.password)}@@HOST@:${local.c.port}/@TARGET@?sslmode=disable"
  dev = "postgres://${local.c.username}:${urlescape(local.c.password)}@@HOST@:${local.c.port}/@DEV@?sslmode=disable"
  src = "file://items_v1.hcl"
}
"""


def test_json_secret_decoded_in_locals(ctx):
    # A login whose password has URL-reserved characters, which must be escaped inside the URL.
    user, password = ctx.role("p@ss:w/rd?x#y%z")
    target, dev = ctx.database(owner=user), ctx.database(owner=user)
    secret = ctx.secret(json.dumps({"username": user, "password": password, "port": int(ctx.server.port)}))
    render(ctx, JSON_PROJECT, SECRET=secret, TARGET=target, DEV=dev)
    ctx.fixture("items_v1.hcl")
    assert_apply_plan(ok(ctx.run("schema", "apply", "--env", "json", "--auto-approve")), CREATE_ITEMS)
    assert_catalog(ctx, target, ITEMS_CATALOG)
    assert ctx.sql(target, "SELECT tableowner FROM pg_tables WHERE tablename = 'items'") == [(user,)]
    assert_hcl(ok(ctx.run("schema", "inspect", "--env", "json")), expected("items.inspect.hcl"))
    assert_synced(ok(ctx.run("schema", "diff", "--env", "json", "--from", "env://src", "--to", "env://url")))


BROKEN_PROJECT = """
data "runtimevar" "missing_secret" {
  url = "awssecretsmanager://@MISSING@?region=us-east-1"
}

data "runtimevar" "missing_param" {
  url = "awsparamstore:///@MISSING@?region=us-east-1"
}

env "broken_url" {
  url = "postgres://@USER@:${data.runtimevar.missing_secret}@@HOST@:@PORT@/@TARGET@?sslmode=disable"
  dev = "@DEVURL@"
}

env "broken_dev" {
  url = "@URL@"
  dev = "postgres://@USER@:${data.runtimevar.missing_param}@@HOST@:@PORT@/@DEV@?sslmode=disable"
}

env "plain" {
  url = "@URL@"
  dev = "@DEVURL@"
  src = "file://items_v1.hcl"
}
"""


def test_unresolvable_data_sources_fail_only_where_used(ctx):
    target, dev = ctx.database(), ctx.database()
    render(ctx, BROKEN_PROJECT, MISSING="sync/never-created-" + target, TARGET=target, DEV=dev,
           URL=ctx.url(target), DEVURL=ctx.url(dev))
    ctx.fixture("items_v1.hcl")
    errors(ctx.run("schema", "inspect", "--env", "broken_url"))
    result = ctx.run("schema", "apply", "--env", "broken_dev", "--to", "file://items_v1.hcl", "--auto-approve")
    errors(result)
    assert ";" not in result.stdout
    assert_catalog(ctx, target, EMPTY_DATABASE)
    # Data sources that the selected environment does not use are not read.
    assert_apply_plan(ok(ctx.run("schema", "apply", "--env", "plain", "--auto-approve")), CREATE_ITEMS)
    assert_catalog(ctx, target, ITEMS_CATALOG)


def test_lookup_timeout(ctx):
    target = ctx.database()
    secret = ctx.secret(ctx.server.password)
    render(ctx, """
data "runtimevar" "pass" {
  url = "awssecretsmanager://@SECRET@?region=us-east-1&timeout=1ns"
}

env "slow" {
  url = "postgres://@USER@:${data.runtimevar.pass}@@HOST@:@PORT@/@TARGET@?sslmode=disable"
}
""", SECRET=secret, TARGET=target)
    errors(ctx.run("schema", "inspect", "--env", "slow"))
    render(ctx, """
data "runtimevar" "pass" {
  url = "awssecretsmanager://@SECRET@?region=us-east-1&timeout=20s"
}

env "slow" {
  url = "postgres://@USER@:${data.runtimevar.pass}@@HOST@:@PORT@/@TARGET@?sslmode=disable"
}
""", SECRET=secret, TARGET=target)
    assert_hcl(ok(ctx.run("schema", "inspect", "--env", "slow")), 'schema "public" {\n  comment = "standard public schema"\n}\n')


def test_environment_selection_and_config_location(ctx):
    target = ctx.database()
    ctx.fixture("items_v1.hcl")
    ok(ctx.run("schema", "apply", "-u", ctx.url(target), "--to", "file://items_v1.hcl", "--auto-approve"))
    # No project file in the working directory.
    errors(ctx.run("schema", "inspect", "--env", "x"))
    url = ctx.url(target).replace(f"/{target}?", '/${getenv("SYNC_DB")}?')
    ctx.write("conf/project.hcl", f'env "x" {{\n  url = "{url}"\n}}\n')
    out = ok(ctx.run("schema", "inspect", "-c", "file://conf/project.hcl", "--env", "x", env={"SYNC_DB": target}))
    assert_hcl(out, expected("items.inspect.hcl"))
    other = ctx.database()
    out = ok(ctx.run("schema", "inspect", "-c", "file://conf/project.hcl", "--env", "x", env={"SYNC_DB": other}))
    assert_hcl(out, 'schema "public" {\n  comment = "standard public schema"\n}\n')
    errors(ctx.run("schema", "inspect", "-c", "file://conf/project.hcl", "--env", "y", env={"SYNC_DB": target}))


SCHEMAS_PROJECT = """
env "narrow" {
  url     = "@URL@"
  dev     = "@DEVURL@"
  src     = "file://billing.hcl"
  schemas = ["public"]
}

env "filtered" {
  url     = "@URL@"
  dev     = "@DEVURL@"
  src     = "file://billing.hcl"
  schemas = ["public", "billing"]
  exclude = ["billing.invoices.amount"]
}
"""


def test_env_schemas_and_exclude(ctx):
    target, dev = ctx.database(), ctx.database()
    render(ctx, SCHEMAS_PROJECT, URL=ctx.url(target), DEVURL=ctx.url(dev))
    ctx.fixture("billing.hcl")
    result = ctx.run("schema", "apply", "--env", "narrow", "--dry-run")
    assert '"billing"' in errors(result)
    assert_catalog(ctx, target, EMPTY_DATABASE)
    out = ok(ctx.run("schema", "apply", "--env", "filtered", "--auto-approve"))
    plan = [
        'CREATE SCHEMA "billing"',
        """COMMENT ON SCHEMA "billing" IS 'money'""",
        """CREATE TABLE "public"."customers" ("id" integer NOT NULL, "name" text NOT NULL, PRIMARY KEY ("id"))""",
        """CREATE TYPE "billing"."currency" AS ENUM ('usd', 'eur')""",
        """CREATE TABLE "billing"."invoices" ("id" bigint NOT NULL, "customer_id" integer NOT NULL,
          "cur" "billing"."currency" NOT NULL DEFAULT 'usd', PRIMARY KEY ("id", "customer_id"),
          CONSTRAINT "inv_customer" FOREIGN KEY ("customer_id") REFERENCES "public"."customers" ("id")
          ON UPDATE CASCADE ON DELETE SET DEFAULT)""",
    ]
    assert_apply_plan(out, plan, [(0, 1), (0, 3), (0, 4), (2, 4), (3, 4)])
    assert_catalog(ctx, target, """
        col|billing|invoices|id|bigint|int8|None|64|0|NO|None
        col|billing|invoices|customer_id|integer|int4|None|32|0|NO|None
        col|billing|invoices|cur|USER-DEFINED|currency|None|None|None|NO|'usd'::billing.currency
        col|public|customers|id|integer|int4|None|32|0|NO|None
        col|public|customers|name|text|text|None|None|None|NO|None
        con|public|customers|customers_pkey|p|PRIMARY KEY (id)
        con|billing|invoices|inv_customer|f|FOREIGN KEY (customer_id) REFERENCES customers(id) ON UPDATE CASCADE ON DELETE SET DEFAULT
        con|billing|invoices|invoices_pkey|p|PRIMARY KEY (id, customer_id)
        idx|billing|invoices_pkey|CREATE UNIQUE INDEX invoices_pkey ON billing.invoices USING btree (id, customer_id)
        idx|public|customers_pkey|CREATE UNIQUE INDEX customers_pkey ON public.customers USING btree (id)
        enum|billing|currency|usd,eur
        schema|billing|money
        schema|public|standard public schema
    """)
    assert_synced(ok(ctx.run("schema", "apply", "--env", "filtered", "--auto-approve")))


TAGS_CATALOG = """
col|public|items|id|integer|int4|None|32|0|NO|None
col|public|tags|id|bigint|int8|None|64|0|NO|None
col|public|tags|label|text|text|None|None|None|NO|None
col|public|tags|scope|text|text|None|None|None|NO|None
con|public|items|items_pkey|p|PRIMARY KEY (id)
con|public|tags|tags_label_scope|u|UNIQUE (label, scope)
con|public|tags|tags_pkey|p|PRIMARY KEY (id)
idx|public|items_pkey|CREATE UNIQUE INDEX items_pkey ON public.items USING btree (id)
idx|public|tags_label_scope|CREATE UNIQUE INDEX tags_label_scope ON public.tags USING btree (label, scope)
idx|public|tags_pkey|CREATE UNIQUE INDEX tags_pkey ON public.tags USING btree (id)
schema|public|standard public schema
"""


def test_env_src_list_combines_files(ctx):
    target, dev = ctx.database(), ctx.database()
    ctx.fixture("items_v1.hcl")
    tags = (FIXTURES / "tags.hcl").read_text()
    ctx.write("tags_only.hcl", tags[tags.index('table "tags"'):])
    ctx.write("atlas.hcl", f'env "e" {{\n  url = "{ctx.url(target)}"\n  dev = "{ctx.url(dev)}"\n'
                           '  src = ["file://items_v1.hcl", "file://tags_only.hcl"]\n}\n')
    plan = CREATE_ITEMS + ["""CREATE TABLE "public"."tags" ("id" bigint NOT NULL GENERATED ALWAYS AS IDENTITY,
        "label" text NOT NULL, "scope" text NOT NULL, PRIMARY KEY ("id"),
        CONSTRAINT "tags_label_scope" UNIQUE ("label", "scope"))"""]
    assert_apply_plan(ok(ctx.run("schema", "apply", "--env", "e", "--auto-approve")), plan)
    assert_catalog(ctx, target, TAGS_CATALOG)
    identity = "SELECT attname, attidentity FROM pg_attribute WHERE attrelid = 'tags'::regclass AND attnum > 0 ORDER BY attnum"
    assert ctx.sql(target, identity) == [("id", "a"), ("label", ""), ("scope", "")]
    assert_synced(ok(ctx.run("schema", "apply", "--env", "e", "--auto-approve")))
