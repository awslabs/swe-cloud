#!/usr/bin/env bash
# Runtime setup for the python:3.12-bookworm solver/verifier image.
# The reference is a statically linked (CGO_ENABLED=0) Linux x86-64 ELF with no
# data assets and no shared-library dependencies; it talks to Kinesis and
# DynamoDB over HTTP. Its observable outputs (JSON lines written by the
# stdout/file outputs, Kinesis records, DynamoDB items) are plain text/JSON
# readable with the Python standard library and the preinstalled boto3, so no
# additional format reader is admitted. Intentionally installs nothing.
set -euo pipefail
python3 -c 'import boto3, json; print("boto3", boto3.__version__)'
