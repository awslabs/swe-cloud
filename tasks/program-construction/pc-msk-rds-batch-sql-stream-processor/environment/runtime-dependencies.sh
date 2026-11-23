#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). Installed for the reference
# and, identically, for the solver and verifier images.
#
# 1. libduckdb v1.5.2 (DuckDB's public C library, the version upstream pins in
#    DUCKDB_VERSION). The program never links DuckDB; it dlopens it through
#    the ADBC driver manager from /usr/local/lib/libduckdb.so, the path
#    upstream's image and install script use. This is a third-party
#    general-purpose SQL engine, not SQLFlow code. The public header goes to
#    /usr/local/include so any language can bind the same engine.
# 2. Pinned general-purpose clients used as supporting infrastructure, not
#    task work: Kafka (confluent-kafka), Postgres (psycopg), DuckDB's Python
#    binding of the same release, YAML and Jinja2 templating.
set -euo pipefail

DUCKDB_VERSION=v1.5.2
DUCKDB_ZIP_SHA256=4711438f0fdb04f0441803409bec5430b763d4f2ac3482c1f97cfa6b5ecb4c15

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL -o "$tmp/libduckdb.zip" \
    "https://github.com/duckdb/duckdb/releases/download/${DUCKDB_VERSION}/libduckdb-linux-amd64.zip"
echo "${DUCKDB_ZIP_SHA256}  $tmp/libduckdb.zip" | sha256sum -c -
python3 -m zipfile -e "$tmp/libduckdb.zip" "$tmp/duckdb"
install -D -m 0644 "$tmp/duckdb/libduckdb.so" /usr/local/lib/libduckdb.so
install -D -m 0644 "$tmp/duckdb/duckdb.h" /usr/local/include/duckdb.h
ldconfig

python3 -m pip install --no-cache-dir \
    'confluent-kafka==2.16.0' \
    'psycopg[binary]==3.3.6' \
    'duckdb==1.5.2' \
    'PyYAML==6.0.3' \
    'Jinja2==3.1.6'
