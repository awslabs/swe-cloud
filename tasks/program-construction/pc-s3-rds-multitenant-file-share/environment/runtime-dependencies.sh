#!/usr/bin/env bash
# Runtime setup for the solver/verifier image (python:3.12-bookworm + boto3).
# plikd itself is a fully static ELF and needs no shared libraries.
# psycopg is installed as general-purpose infrastructure so tests/solvers can
# inspect the Postgres metadata database that the program writes (RDS engine).
set -euo pipefail
python3 -m pip install --no-cache-dir 'psycopg[binary]==3.3.9'
python3 -c 'import psycopg, boto3; print("psycopg", psycopg.__version__, "boto3", boto3.__version__)'
