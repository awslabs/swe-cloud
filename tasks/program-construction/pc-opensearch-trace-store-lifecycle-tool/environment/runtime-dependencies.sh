#!/usr/bin/env bash
# Runtime setup for the python:3.12-bookworm solver/verifier images (network available).
# The reference is a static CGO-free ELF: it needs no system libraries. Installed here is
# general-purpose supporting infrastructure only:
#   - opensearch-py: pinned client for inspecting OpenSearch state (templates, indices,
#     aliases, documents). OpenSearch's REST API is not the task's subject; reading it is
#     infrastructure.
#   - curl is already present in the base image (used for plain HTTP checks).
# OTLP/HTTP accepts JSON (OTLP JSON encoding), which the standard library can produce, so
# no protobuf/OTLP SDK is required.
set -euo pipefail
python3 -m pip install --no-cache-dir --disable-pip-version-check opensearch-py==3.2.0
python3 -c 'import opensearchpy, boto3; print("opensearch-py", opensearchpy.__versionstr__, "boto3", boto3.__version__)'
command -v curl
