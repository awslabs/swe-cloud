#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). The reference program is a
# fully static ELF and needs no shared libraries. Installed here are only the
# admitted general-purpose clients used to inspect graded state:
#   psycopg (Postgres wire client) and redis-py (Valkey/Redis RESP client).
# No upstream binary, source or alias is installed.
set -euxo pipefail
python3 -m pip install --no-cache-dir "psycopg[binary]==3.2.10" "redis==5.2.1"
python3 -c 'import psycopg, redis; print("psycopg", psycopg.__version__, "redis", redis.__version__)'
