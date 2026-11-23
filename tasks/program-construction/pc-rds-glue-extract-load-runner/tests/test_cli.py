"""Invocation surface: single-stream flags, connection sources, stream selection and exit status.
Expectations observed from the reference (see curation.md)."""

import datetime as dt

from harness import give_to_candidate, run_program, write_fixture
from synclab import ENDPOINT, create_orders, csv_table, failed, ok


def single(lab, stream, obj, *extra, tgt="TGT"):
    return lab.run("run", "--src-conn", "SRC", "--src-stream", stream, "--tgt-conn", tgt, "--tgt-object", obj, *extra)


def test_single_stream_flags(lab):
    create_orders(lab, "(1,'ann',10.50,'2024-01-01 10:00'),(2,'bob',20.00,'2024-01-02 10:00'),"
                       "(3,'cy',null,'2024-01-03 10:00'),(4,'dee',4,'2024-01-04')")
    ok(single(lab, "sales.orders", "cli.flags", "--select", "id,customer", "--where", "id > 1",
              "--tgt-options", '{"column_casing": "upper"}'))
    assert [c[:2] for c in lab.tgt_columns("cli", "flags")] == [("ID", "integer"), ("CUSTOMER", "text")]
    assert lab.tgt_rows('cli.flags') == [(2, "bob"), (3, "cy"), (4, "dee")]
    ok(single(lab, "sales.orders", "cli.limited", "--limit", "2"))
    rows = lab.tgt_rows("cli.limited", "id")
    assert len(rows) == 2 and set(rows) <= {(1,), (2,), (3,), (4,)}
    ok(single(lab, "sales.orders", "cli.keyed", "--src-options", '{"limit": 1}',
              "--tgt-options", "table_keys: {primary: [id]}"))
    assert len(lab.tgt_rows("cli.keyed")) == 1
    assert lab.tgt("select count(*) from pg_constraint where conrelid = 'cli.keyed'::regclass and contype = 'p'") == [(1,)]


def test_inline_sql_and_incremental_flags(lab):
    create_orders(lab)
    ok(single(lab, "select id, customer from sales.orders where id < 3", "cli.q"))
    assert [c[:2] for c in lab.tgt_columns("cli", "q")] == [("id", "integer"), ("customer", "text")]
    assert lab.tgt_rows("cli.q") == [(1, "ann"), (2, "bob")]
    args = ("--mode", "incremental", "--primary-key", "id", "--update-key", "updated_at")
    ok(single(lab, "sales.orders", "cli.inc", *args))
    lab.src("update sales.orders set customer = 'ann2', updated_at = '2024-02-01' where id = 1;"
            " update sales.orders set customer = 'bob2' where id = 2")
    ok(single(lab, "sales.orders", "cli.inc", *args))
    assert lab.tgt_rows("cli.inc", "id, customer, updated_at") == [
        (1, "ann2", dt.datetime(2024, 2, 1)), (2, "bob", dt.datetime(2024, 1, 2, 10)), (3, "cy", dt.datetime(2024, 1, 3, 10))]


def test_connections_from_env_yaml(lab):
    create_orders(lab)
    sling_home = lab.home / ".sling"
    sling_home.mkdir()
    give_to_candidate(sling_home)
    write_fixture(sling_home / "env.yaml", f"""connections:
  WAREHOUSE:
    url: "{lab.connections()['TGT']}"
  STORE:
    type: s3
    bucket: {lab.bucket}
    endpoint: {ENDPOINT}
    region: us-east-1
  LAKE2:
    type: iceberg
    catalog_type: glue
    glue_warehouse: s3://{lab.bucket}/warehouse
    s3_region: us-east-1
    s3_endpoint: {ENDPOINT}
""")
    ok(single(lab, "sales.orders", "viaenv.orders", tgt="WAREHOUSE"))
    assert lab.tgt_rows("viaenv.orders", "id") == [(1,), (2,), (3,)]
    ok(single(lab, "sales.orders", "viaenv/orders.csv", tgt="STORE"))
    assert sorted(lab.objects()) == ["viaenv/orders.csv"]
    ok(single(lab, "select 4 as x", f"{lab.glue_db}.from_envyaml", tgt="LAKE2"))
    assert lab.iceberg("from_envyaml").rows == [{"x": 4}]


def test_connection_variables_in_yaml_form(lab):
    create_orders(lab)
    env = lab.env()
    env["YTGT"] = "type: postgres\nurl: " + lab.connections()["TGT"]
    env["YSTORE"] = f"type: s3\nbucket: {lab.bucket}\nendpoint: {ENDPOINT}\nregion: us-east-1"
    spec = lab.root / "y.yaml"
    write_fixture(spec, """
source: SRC
target: YTGT
streams:
  CAPS_Q:
    sql: select 1 as x
    object: 'yc.{stream_name}'
""")
    ok(run_program(["run", "-r", str(spec)], cwd=lab.root, env=env, timeout=45))
    assert lab.tgt_tables("yc") == ["caps_q"]
    assert lab.tgt_rows("yc.caps_q") == [(1,)]
    ok(run_program(["run", "--src-conn", "SRC", "--src-stream", "select 3 as x", "--tgt-conn", "YSTORE",
                    "--tgt-object", "yaml/x.csv"], cwd=lab.root, env=env, timeout=45))
    objs = lab.objects()
    assert sorted(objs) == ["yaml/x.csv"]
    assert csv_table(objs["yaml/x.csv"]) == (["x"], [("3",)])


def test_streams_flag_and_disabled_streams(lab):
    create_orders(lab)
    lab.src("create table sales.customers (id int); insert into sales.customers values (1);"
            " create table sales.items (id int); insert into sales.items values (1)")
    spec = """
source: SRC
target: TGT
defaults:
  object: 'st.{stream_table}'
streams:
  sales.customers:
  sales.orders:
  sales.items:
    disabled: true
"""
    ok(lab.replicate(spec, "--streams", "sales.orders"))
    assert lab.tgt_tables("st") == ["orders"]
    ok(lab.replicate(spec))
    assert lab.tgt_tables("st") == ["customers", "orders"]


def test_conns_test_reports_reachability(lab):
    for name in ("SRC", "TGT", "LAKE", "BUCKET"):
        ok(lab.run("conns", "test", name))
    failed(lab.run("conns", "test", "NOPE"))


def test_invalid_invocations_fail_without_writing(lab):
    create_orders(lab)
    failed(single(lab, "sales.orders", "bad.x", tgt="NOPE"))
    failed(lab.run("run", "-r", str(lab.root / "does-not-exist.yaml")))
    failed(single(lab, "sales.orders", "bad.mode", "--mode", "upsertish"))
    failed(single(lab, "sales.orders", "bad.nokeys", "--mode", "incremental"))
    failed(single(lab, "select id, updated_at from sales.orders", "bad.noplaceholder",
                  "--mode", "incremental", "--update-key", "updated_at"))
    assert lab.tgt_tables("bad") == []


def test_failed_stream_does_not_stop_later_streams(lab):
    create_orders(lab)
    result = failed(lab.replicate("""
source: SRC
target: TGT
defaults:
  object: 'mixed.{stream_table}'
streams:
  sales.missing:
  sales.orders:
"""))
    assert result.returncode != 0
    assert lab.tgt_tables("mixed") == ["orders"]
    assert len(lab.tgt_rows("mixed.orders")) == 3


def test_unmatched_wildcard_is_not_an_error(lab):
    create_orders(lab)
    ok(lab.replicate("""
source: SRC
target: TGT
streams:
  nothere.*:
    object: 'nm.{stream_table}'
"""))
    assert lab.tgt_tables("nm") == []
