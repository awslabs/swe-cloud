#!/usr/bin/env bash
# Runtime setup for the solver/verifier images (python:3.12-bookworm + boto3).
# The reference ELF is static (CGO disabled) and needs no extra system libraries;
# it talks to AWS only over HTTP(S) via the configured custom endpoints.
# Supporting infrastructure: the program's configuration is YAML, and Python's
# standard library has no YAML reader, so a pinned general-purpose YAML parser is
# installed. Reading YAML is infrastructure; the configuration language built on
# top of it (accounts, filters, presets, ...) is the intended task work.
set -euo pipefail
python3 -m pip install --no-cache-dir 'PyYAML==6.0.2'
python3 -c 'import yaml, boto3; print("yaml", yaml.__version__, "boto3", boto3.__version__)'
