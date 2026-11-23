"""Per-test isolated resources and read-only inspectors for each target kind, plus small
comparison helpers. Expected values live in the tests; nothing here models the program."""

import csv
import datetime as dt
import gzip
import io
import json
import uuid
from decimal import Decimal

import boto3
import psycopg

from harness import give_to_candidate, run_program, write_fixture
from iceberg_reader import read_table

ENDPOINT = "http://engines:4566"
PASSWORD = "sync-pass-1"


class Lab:
    """One test's isolated source/target databases, bucket and Glue namespace."""

    def __init__(self, engine, root):
        tag = uuid.uuid4().hex[:10]
        self.engine, self.root = engine, root
        self.src_db, self.tgt_db = "src_" + tag, "tgt_" + tag
        self.bucket = "sync-" + tag
        self.glue_db = "lake_" + tag
        self.home = root / "home"
        self.created_dbs = []

    def setup(self):
        self.home.mkdir()
        give_to_candidate(self.home)
        with self._connect("postgres") as c:
            for db in (self.src_db, self.tgt_db):
                c.execute(f'create database "{db}"')
                self.created_dbs.append(db)
        boto3.client("s3").create_bucket(Bucket=self.bucket)

    # --- connections -------------------------------------------------
    def _url(self, db):
        e = self.engine
        return f"postgresql://app:{PASSWORD}@{e['host']}:{e['port']}/{db}?sslmode=disable"

    def connections(self):
        return {
            "SRC": self._url(self.src_db),
            "TGT": self._url(self.tgt_db),
            "LAKE": json.dumps({"type": "iceberg", "catalog_type": "glue",
                                "glue_warehouse": f"s3://{self.bucket}/warehouse",
                                "s3_region": "us-east-1", "s3_endpoint": ENDPOINT}),
            "BUCKET": json.dumps({"type": "s3", "bucket": self.bucket, "endpoint": ENDPOINT,
                                  "region": "us-east-1"}),
        }

    def env(self, connections=True):
        env = {"HOME": str(self.home), "SLING_HOME_DIR": str(self.home / ".sling"),
               "SLING_DISABLE_TELEMETRY": "true"}
        if connections:
            env.update(self.connections())
        return env

    # --- running the program ----------------------------------------
    def run(self, *args, connections=True, timeout=45):
        return run_program(list(args), cwd=self.root, env=self.env(connections), timeout=timeout)

    def replicate(self, text, *extra, name="replication.yaml"):
        path = self.root / name
        write_fixture(path, text)
        return self.run("run", "-r", str(path), *extra)

    # --- PostgreSQL --------------------------------------------------
    def _connect(self, db):
        e = self.engine
        return psycopg.connect(host=e["host"], port=e["port"], user="app", password=PASSWORD, dbname=db,
                               sslmode="disable", autocommit=True)

    def src(self, sql, params=None):
        return self._sql(self.src_db, sql, params)

    def tgt(self, sql, params=None):
        return self._sql(self.tgt_db, sql, params)

    def _sql(self, db, sql, params):
        with self._connect(db) as c:
            cur = c.execute(sql, params)
            return cur.fetchall() if cur.description else None

    def tgt_columns(self, schema, table):
        """[(name, data_type, char_max_length, numeric_precision, numeric_scale)] in table order."""
        return [tuple(r) for r in self.tgt(
            "select column_name, data_type, character_maximum_length,"
            " case when data_type = 'numeric' then numeric_precision end,"
            " case when data_type = 'numeric' then numeric_scale end"
            " from information_schema.columns where table_schema = %s and table_name = %s"
            " order by ordinal_position", (schema, table))]

    def tgt_tables(self, schema):
        return sorted(r[0] for r in self.tgt(
            "select table_name from information_schema.tables where table_schema = %s", (schema,)))

    def tgt_rows(self, qualified, columns="*"):
        return sorted_rows(_strict_cells(r) for r in self.tgt(f"select {columns} from {qualified}"))

    # --- S3 ----------------------------------------------------------
    def objects(self, prefix=""):
        """{key: bytes} for every object under prefix (Iceberg warehouse excluded)."""
        s3 = boto3.client("s3")
        out = {}
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                if o["Key"].startswith("warehouse/"):
                    continue
                out[o["Key"]] = s3.get_object(Bucket=self.bucket, Key=o["Key"])["Body"].read()
        return out

    def put(self, key, body=b"sentinel"):
        boto3.client("s3").put_object(Bucket=self.bucket, Key=key, Body=body)

    # --- Iceberg -----------------------------------------------------
    def glue_table(self, table, database=None):
        return boto3.client("glue").get_table(DatabaseName=database or self.glue_db, Name=table)["Table"]

    def glue_tables(self, database=None):
        glue = boto3.client("glue")
        try:
            return sorted(t["Name"] for t in glue.get_tables(DatabaseName=database or self.glue_db)["TableList"])
        except glue.exceptions.EntityNotFoundException:
            return None

    def metadata_location(self, table, database=None):
        return self.glue_table(table, database)["Parameters"]["metadata_location"]

    def iceberg(self, table, database=None):
        return read_table(self.metadata_location(table, database))

    def cleanup(self):
        glue, s3 = boto3.client("glue"), boto3.client("s3")
        try:
            for d in glue.get_databases()["DatabaseList"]:
                if d["Name"].startswith(self.glue_db):
                    for t in glue.get_tables(DatabaseName=d["Name"]).get("TableList", []):
                        glue.delete_table(DatabaseName=d["Name"], Name=t["Name"])
                    glue.delete_database(Name=d["Name"])
        except Exception as exc:  # noqa: BLE001 -- cleanup is best effort
            print("glue cleanup:", exc)
        try:
            while True:
                objs = s3.list_objects_v2(Bucket=self.bucket).get("Contents", [])
                if not objs:
                    break
                s3.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": o["Key"]} for o in objs]})
            s3.delete_bucket(Bucket=self.bucket)
        except Exception as exc:  # noqa: BLE001
            print("s3 cleanup:", exc)
        try:
            with self._connect("postgres") as c:
                for db in self.created_dbs:
                    c.execute(f'drop database if exists "{db}" with (force)')
        except Exception as exc:  # noqa: BLE001
            print("postgres cleanup:", exc)


# --- small comparison helpers ----------------------------------------------------

def _canonical(v):
    # Equal values must get equal keys whatever their representation (Decimal scale, int/float).
    if v is None:
        return ("0",)
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float, Decimal)):
        return ("n", Decimal(str(v)).normalize())
    if isinstance(v, dt.datetime) and v.tzinfo is not None:
        return ("t", v.astimezone(dt.timezone.utc).isoformat())
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return ("t", v.isoformat())
    if isinstance(v, JsonCell):
        v = v.value
    if isinstance(v, (dict, list)):
        return ("j", json.dumps(v, sort_keys=True, default=str))
    return ("s", type(v).__name__, str(v))


class JsonCell:
    """A decoded json/jsonb cell that equals expected JSON only under same_json (true != 1)."""

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        other = other.value if isinstance(other, JsonCell) else other
        return isinstance(other, (dict, list)) and same_json(self.value, other)

    def __repr__(self):
        return f"JsonCell({self.value!r})"


def _strict_cells(row):
    return tuple(JsonCell(v) if isinstance(v, (dict, list)) else v for v in row)


def _sort_key(row):
    return tuple(_canonical(v) for v in row)


def sorted_rows(rows):
    """Rows as a sorted list of tuples: an order-free multiset comparison."""
    return sorted((tuple(r) for r in rows), key=_sort_key)


def dict_rows(rows):
    """Iceberg rows (dicts) as a sorted list of item tuples."""
    return sorted((tuple(sorted(r.items())) for r in rows), key=lambda r: _sort_key([v for _, v in r]))


def csv_records(data):
    """Parse CSV bytes (RFC 4180 quoting) into a list of rows of cell text."""
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


def csv_table(data):
    """(header, sorted body rows) of a CSV file with a header line."""
    records = csv_records(data)
    assert records, "empty CSV file"
    return records[0], sorted(tuple(r) for r in records[1:])


def jsonl_records(data):
    # Numbers decode as Decimal so value comparison is exact (9007199254740993.0 == 9007199254740993).
    return [json.loads(line, parse_float=Decimal, parse_int=Decimal)
            for line in data.decode("utf-8").splitlines() if line.strip()]


def gunzip(data):
    return gzip.decompress(data)


def same_json(actual, expected):
    """JSON equality that keeps booleans, numbers, strings and null distinct; numbers by value."""
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        return type(actual) is type(expected) and actual == expected
    if isinstance(expected, (int, float, Decimal)):
        return type(actual) in (int, float, Decimal) and Decimal(str(actual)) == Decimal(str(expected))
    if isinstance(expected, list):
        return type(actual) is list and len(actual) == len(expected) and all(
            same_json(a, e) for a, e in zip(actual, expected))
    if isinstance(expected, dict):
        return type(actual) is dict and actual.keys() == expected.keys() and all(
            same_json(actual[k], v) for k, v in expected.items())
    raise TypeError(expected)


def same_json_multiset(actual, expected):
    """Every expected record matched by exactly one actual record (order-free)."""
    remaining = list(actual)
    if len(remaining) != len(expected):
        return False
    for e in expected:
        for i, a in enumerate(remaining):
            if same_json(a, e):
                del remaining[i]
                break
        else:
            return False
    return True


def ok(result):
    assert result.returncode == 0, f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def failed(result):
    assert result.returncode != 0, f"expected failure\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


TYPED_DDL = (
    "create schema sales; create table sales.typed (id int primary key, s smallint, b bigint,"
    " n numeric(10,2), r real, d double precision, flag boolean, t text, v varchar(20), ch char(3),"
    " dt date, ts timestamp, tz timestamptz, j jsonb, js json, u uuid, tm time, by bytea)"
)
TYPED_ROWS = (
    "insert into sales.typed values (1, 7, 9007199254740993, 10.50, 1.5, 2.25, true, 'he said \"hi\", ok',"
    " 'v1', 'ab', '2024-03-04', '2024-01-02 10:00:00.123456', '2024-01-02 10:00:00+02',"
    " '{\"a\":[1,2],\"b\":null}', '{\"x\":1}', 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', '13:14:15', '\\x0102'),"
    " (2, null, null, null, null, null, false, E'line1\\nline2', null, null, null, null, null, null, null,"
    " null, null, null)"
)


def create_typed(lab):
    """A source table covering the admitted PostgreSQL column types, one full row and one sparse row."""
    lab.src(TYPED_DDL)
    lab.src(TYPED_ROWS)


ORDERS_DDL = ("create schema if not exists sales; create table sales.orders (id int primary key, customer text,"
              " amount numeric(10,2), updated_at timestamp)")


def create_orders(lab, rows="(1,'ann',10.50,'2024-01-01 10:00'),(2,'bob',20.00,'2024-01-02 10:00'),"
                            "(3,'cy',null,'2024-01-03 10:00')"):
    lab.src(ORDERS_DDL)
    lab.src("insert into sales.orders values " + rows)
