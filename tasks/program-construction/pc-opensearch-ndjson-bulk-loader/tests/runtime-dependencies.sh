#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). The reference itself is a
# static Go ELF with no shared-library needs. A pinned general-purpose
# OpenSearch client is installed as supporting infrastructure so that tests and
# solvers can inspect index state (documents, mappings, settings) the same way.
set -euo pipefail
python3 -m pip install --no-cache-dir "opensearch-py==2.8.0"
python3 -c "import opensearchpy, boto3; print('opensearch-py', opensearchpy.__versionstr__)"
