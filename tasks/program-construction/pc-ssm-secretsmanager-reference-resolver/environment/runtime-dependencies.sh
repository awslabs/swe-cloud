#!/usr/bin/env bash
# Runtime for python:3.12-bookworm (+boto3). The reference is a static ELF and
# needs no runtime libraries. YAML parsing is supporting infrastructure (the
# task's documents are YAML/JSON), so a pinned general-purpose YAML reader is
# installed for fixture generation, verification and solver use.
set -euo pipefail
python3 -m pip install --no-cache-dir 'PyYAML==6.0.2'
python3 -c 'import yaml, boto3; print("PyYAML", yaml.__version__, "boto3", boto3.__version__)'
