#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3), shared by solver and verifier images.
# The reference ELF needs only glibc/libm/libgcc_s from the base image.
# Admitted general-purpose format/client libraries (supporting infrastructure, not the
# task subject): psycopg for Postgres, pyarrow for Parquet, pyiceberg for Iceberg table
# metadata/manifests on a Glue catalog.
set -euo pipefail
python3 -m pip install --no-cache-dir \
  'psycopg[binary]==3.3.6' \
  'pyarrow==26.0.0' \
  'pyiceberg==0.12.0'
python3 -c 'import psycopg, pyarrow, pyiceberg, boto3; print("runtime ok", pyiceberg.__version__, pyarrow.__version__, psycopg.__version__)'
