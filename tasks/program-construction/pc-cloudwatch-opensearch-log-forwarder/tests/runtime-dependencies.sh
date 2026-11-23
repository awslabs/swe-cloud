#!/usr/bin/env bash
# Runtime/solver/verifier setup on python:3.12-bookworm (boto3 preinstalled).
# The reference program is a static Go ELF and needs no system libraries.
# opensearch-py is admitted general-purpose infrastructure for inspecting the
# OpenSearch documents (graded state); it implements none of the subject behavior.
set -euo pipefail
python3 -m pip install --no-cache-dir "opensearch-py==2.8.0"
python3 -c 'import opensearchpy, boto3; print("opensearch-py", opensearchpy.__versionstr__)'
