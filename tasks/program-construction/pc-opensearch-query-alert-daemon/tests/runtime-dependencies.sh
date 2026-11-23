#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). The reference is a static
# CGO-free Go ELF and needs no system libraries; ca-certificates are already in
# the base image. opensearch-py is installed as a pinned general-purpose client
# so fixtures/verifiers (and solvers) can talk to the Floci-provisioned
# OpenSearch domain; it is supporting infrastructure, not task work.
set -euo pipefail
python3 -m pip install --no-cache-dir --disable-pip-version-check \
  "opensearch-py==2.8.0"
python3 -c 'import opensearchpy, boto3; print("opensearch-py", opensearchpy.__versionstr__)'
