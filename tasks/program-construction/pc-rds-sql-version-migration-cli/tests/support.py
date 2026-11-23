"""Small helpers for invoking the program and inspecting database / S3 state."""

import concurrent.futures
import uuid
from pathlib import Path
from urllib.parse import urlencode

import boto3
from harness import give_to_candidate, run_program, write_fixture
from psycopg import sql

BOOKKEEPING = "schema_migrations"


class Database:
    def __init__(self, engine, name):
        self.engine, self.name = engine, name

    def url(self, scheme="postgres", password="app-pass-1", user="app", **params):
        query = urlencode({"sslmode": "disable", **params})
        return f"{scheme}://{user}:{password}@{self.engine.host}:{self.engine.port}/{self.name}?{query}"

    def query(self, statement, params=None):
        with self.engine.connect(self.name) as conn:
            return conn.execute(statement, params).fetchall()

    def execute(self, statement):
        with self.engine.connect(self.name, autocommit=True) as conn:
            conn.execute(statement)

    def tables(self, schema="public"):
        """Base-table names in one schema, sorted by code point."""
        rows = self.query("SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,))
        return sorted(name for (name,) in rows)

    def user_tables(self, bookkeeping=BOOKKEEPING, schema="public"):
        """Tables other than the bookkeeping table (its creation time is not graded)."""
        return [name for name in self.tables(schema) if name != bookkeeping]

    def versions(self, table=BOOKKEEPING, schema="public"):
        """Bookkeeping rows as (version, dirty); an absent table reads as nil (no rows)."""
        exists = self.query("SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s", (schema, table))
        if not exists:
            return []
        rows = self.query(sql.SQL("SELECT version, dirty FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
        for version, dirty in rows:
            assert type(version) is int, f"version column holds {type(version).__name__}"
            assert type(dirty) is bool, f"dirty column holds {type(dirty).__name__}"
        return rows

    def add_journal(self):
        self.execute("CREATE TABLE public.journal (seq serial PRIMARY KEY, step text NOT NULL)")

    def journal(self):
        return [step for (step,) in self.query("SELECT step FROM public.journal ORDER BY seq")]

    def set_role_search_path(self, role, path):
        """Server-side session default; the venue does not forward client startup parameters."""
        self.execute(f'ALTER ROLE "{role}" IN DATABASE "{self.name}" SET search_path = {path}')


def journal(step):
    """Migration body that records that it ran."""
    return f"INSERT INTO public.journal (step) VALUES ('{step}');\n"


def write_migrations(root, files, name="migrations"):
    """Write {filename: sql} into a candidate-readable directory under root."""
    directory = Path(root) / name
    if not files:
        directory.mkdir(mode=0o755)
        give_to_candidate(directory)
    for filename, body in files.items():
        write_fixture(directory / filename, body)
    return directory


def migrate(source, url, *args, stdin=None, timeout=30, cwd=None):
    """Run the program against a file directory (Path) or a source URL (str)."""
    location = ["-path", str(source)] if isinstance(source, Path) else ["-source", source]
    return run_program([*location, "-database", url, *args], stdin=stdin, timeout=timeout, cwd=cwd)


def ok(result):
    assert result.returncode == 0, f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def fails(result):
    assert result.returncode != 0, f"expected failure\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


class Bucket:
    def __init__(self):
        self.s3 = boto3.client("s3")
        self.name = "svr-" + uuid.uuid4().hex[:16]
        self.s3.create_bucket(Bucket=self.name)

    def put(self, objects, prefix=""):
        for key, body in objects.items():
            self.s3.put_object(Bucket=self.name, Key=prefix + key, Body=body.encode())

    def put_many(self, objects):
        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            list(pool.map(lambda item: self.s3.put_object(Bucket=self.name, Key=item[0], Body=item[1].encode()), objects.items()))

    def keys(self):
        found = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.name):
            found.extend(item["Key"] for item in page.get("Contents", []))
        return sorted(found)

    def url(self, prefix=""):
        return f"s3://{self.name}/{prefix}" if prefix else f"s3://{self.name}"

    def destroy(self):
        try:
            keys = self.keys()
            with concurrent.futures.ThreadPoolExecutor(16) as pool:
                list(pool.map(lambda key: self.s3.delete_object(Bucket=self.name, Key=key), keys))
            self.s3.delete_bucket(Bucket=self.name)
        except Exception as error:  # noqa: BLE001 -- cleanup must not mask the test result
            print(f"bucket cleanup failed for {self.name}: {error}")
