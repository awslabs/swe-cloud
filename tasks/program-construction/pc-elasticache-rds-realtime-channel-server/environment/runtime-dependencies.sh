#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (solver, verifier and probe images).
# The payload is a self-contained ELF needing only glibc/libgcc (OpenSSL is linked
# statically). Installed here are pinned, general-purpose client libraries used to
# observe the venue: a Postgres client (psycopg), a Redis/Valkey client (redis-py) and
# a WebSocket client (websockets) for speaking the Pusher protocol. boto3 is already
# present. No upstream code or binary is installed.
set -euxo pipefail
python3 -m pip install --no-cache-dir \
  'psycopg[binary]==3.3.6' \
  'redis==8.1.0' \
  'websockets==17.3'
python3 -c 'import psycopg, redis, websockets, boto3; print(psycopg.__version__, redis.__version__, websockets.__version__, boto3.__version__)'
