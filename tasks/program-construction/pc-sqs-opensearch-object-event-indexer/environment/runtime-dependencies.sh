#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3).
# The reference is a static musl ELF: it needs no shared libraries. It speaks plain
# HTTP to Floci (S3/SQS) and to the OpenSearch domain endpoint, so no CA bundle or
# TLS assets are required beyond the image's own ca-certificates.
# opensearch-py is installed as a pinned general-purpose client for inspecting index
# state from tests/probes (supporting infrastructure, not task work). zstandard is a
# pinned general-purpose zstd codec: Python 3.12 has gzip in the stdlib but no zstd, and
# decompressing a standard container format is infrastructure for this task, not the
# behaviour under reconstruction (which is the detection/selection of compression). No upstream
# binary, source or aliases are installed.
set -euo pipefail
python3 -m pip install --no-cache-dir --disable-pip-version-check opensearch-py==2.8.0 zstandard==0.23.0
