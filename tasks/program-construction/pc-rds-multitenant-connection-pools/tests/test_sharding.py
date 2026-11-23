"""Pools with several shards: explicit shard selection, sharding keys, comment routing and default_shard."""

from collections import Counter

import psycopg
import pytest

from conftest import marker, run_pooler, show_rows, tenant_pools

KEYS = [0, 1, 2, 3, 4, 5, 10, 77, 100, 4096, 12345678901]


def sharded_pools(tenants, settings=None, count=3):
    engine = tenants.engines[0]
    pools = tenant_pools(tenants, alpha_settings=settings)
    pools["alpha"]["shards"] = [{"servers": [[engine.engine_host, engine.engine_port, "primary"]], "database": name}
                                for name in tenants.shard_databases(count)]
    return pools


def hash_partitions(tenants, keys, count=3):
    """The partition PostgreSQL's own HASH partitioning picks for each bigint key, as {key: index}."""
    table = f"{tenants.prefix}_hp"
    with tenants.engines[0].master() as conn:
        conn.execute(f"CREATE TABLE {table}(id bigint) PARTITION BY HASH (id)")
        try:
            for index in range(count):
                conn.execute(f"CREATE TABLE {table}_{index} PARTITION OF {table} "
                             f"FOR VALUES WITH (MODULUS {count}, REMAINDER {index})")
            conn.execute(f"INSERT INTO {table} VALUES " + ",".join(f"({key})" for key in keys))
            rows = conn.execute(f"SELECT id, tableoid::regclass::text FROM {table}").fetchall()
        finally:
            conn.execute(f"DROP TABLE {table}")
    return {key: int(name.rsplit("_", 1)[1]) for key, name in rows}


def test_clients_without_a_shard_use_shard_zero(tenants):
    with run_pooler(pools=sharded_pools(tenants)) as pooler:
        with pooler.alpha() as conn:
            assert Counter(marker(conn) for _ in range(10)) == Counter({"s0": 10})


def test_set_shard_routes_to_that_shard(tenants):
    with run_pooler(pools=sharded_pools(tenants)) as pooler:
        with pooler.alpha() as conn:
            for index in (2, 0, 1):
                conn.execute(f"SET SHARD TO '{index}'")
                assert [marker(conn) for _ in range(3)] == [f"s{index}"] * 3
                assert conn.execute("SELECT current_database()::text").fetchall() == [(f"{tenants.prefix}_s{index}",)]


def test_shard_beyond_the_last_is_refused(tenants):
    with run_pooler(pools=sharded_pools(tenants)) as pooler:
        with pooler.alpha() as conn:
            conn.execute("SET SHARD TO '1'")
            with pytest.raises(psycopg.Error) as failed:
                conn.execute("SET SHARD TO '3'")
            assert failed.value.sqlstate == "58000"
            assert conn.execute("SHOW SHARD").fetchall() == [("1",)]
            assert marker(conn) == "s1"


def test_sharding_key_picks_the_hash_partition_shard(tenants):
    with run_pooler(pools=sharded_pools(tenants)) as pooler:
        expected = hash_partitions(tenants, KEYS)
        with pooler.alpha() as conn:
            seen = {}
            for key in KEYS:
                assert conn.execute(f"SET SHARDING KEY TO '{key}'").statusmessage == "SET SHARDING KEY"
                seen[key] = marker(conn)
        assert seen == {key: f"s{index}" for key, index in expected.items()}
        assert len(set(seen.values())) == 3


def test_show_shard_reports_the_shard_of_a_sharding_key(tenants):
    with run_pooler(pools=sharded_pools(tenants)) as pooler:
        expected = hash_partitions(tenants, [10, 77])
        with pooler.alpha() as conn:
            for key in (10, 77):
                conn.execute(f"SET SHARDING KEY TO {key}")
                assert conn.execute("SHOW SHARD").fetchall() == [(str(expected[key]),)]


def test_shard_id_comment_routes_the_query(tenants):
    settings = {"shard_id_regex": r"/\* shard_id: (\d+) \*/"}
    with run_pooler(pools=sharded_pools(tenants, settings)) as pooler:
        with pooler.alpha() as conn:
            assert conn.execute("/* shard_id: 2 */ SELECT tenant FROM marker").fetchall() == [("s2",)]
            assert conn.execute("/* shard_id: 1 */ SELECT tenant FROM marker").fetchall() == [("s1",)]
            # The selection stays for later queries without a comment.
            assert marker(conn) == "s1"


def test_sharding_key_comment_routes_the_query(tenants):
    settings = {"sharding_key_regex": r"/\* sharding_key: (\d+) \*/"}
    with run_pooler(pools=sharded_pools(tenants, settings)) as pooler:
        expected = hash_partitions(tenants, [3, 4096])
        with pooler.alpha() as conn:
            for key in (3, 4096):
                rows = conn.execute(f"/* sharding_key: {key} */ SELECT tenant FROM marker").fetchall()
                assert rows == [(f"s{expected[key]}",)]


def test_default_shard_routes_clients_without_a_shard(tenants):
    with run_pooler(pools=sharded_pools(tenants, {"default_shard": "shard_2"})) as pooler:
        with pooler.alpha() as conn:
            assert Counter(marker(conn) for _ in range(10)) == Counter({"s2": 10})
            assert conn.execute("SHOW SHARD").fetchall() == [("unset",)]


def test_random_default_shard_uses_every_shard(tenants):
    with run_pooler(pools=sharded_pools(tenants, {"default_shard": "random"})) as pooler:
        with pooler.alpha() as conn:
            assert set(marker(conn) for _ in range(60)) == {"s0", "s1", "s2"}


def test_automatic_sharding_key_routes_inserts(tenants):
    settings = {"query_parser_enabled": True, "query_parser_read_write_splitting": True,
                "automatic_sharding_key": "data.id"}
    with run_pooler(pools=sharded_pools(tenants, settings)) as pooler:
        keys = [1, 2, 3, 4, 5, 10]
        expected = hash_partitions(tenants, keys)
        for key in keys:
            with pooler.alpha() as conn:
                conn.execute(f"INSERT INTO data (id, v) VALUES ({key}, 'v{key}')")
    for index in range(3):
        with tenants.engines[0].direct(f"{tenants.prefix}_s{index}", tenants.alpha.owner,
                                       tenants.alpha.owner_password) as conn:
            stored = [row[0] for row in conn.execute("SELECT id FROM data ORDER BY id").fetchall()]
        assert stored == [key for key in keys if expected[key] == index]


def test_show_databases_lists_every_shard(tenants):
    with run_pooler(pools=sharded_pools(tenants)) as pooler:
        with pooler.admin() as admin:
            rows = show_rows(admin, "SHOW DATABASES")[1]
    alpha = sorted((row[0], row[3]) for row in rows if row[0].startswith("alpha"))
    assert alpha == [(f"alpha_shard_{index}_primary", f"{tenants.prefix}_s{index}") for index in range(3)]
