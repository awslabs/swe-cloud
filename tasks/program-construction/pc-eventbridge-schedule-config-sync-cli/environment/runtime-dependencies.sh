#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). The reference itself is a
# static ELF and needs nothing beyond the base image (ca-certificates/tzdata
# are already present; tz data is also embedded as a fallback).
#
# Admitted general-purpose format infrastructure for the solver/verifier:
#   PyYAML 6.0.3     - YAML reading/writing (config input, dump output).
#   gojsonnet 0.22.0 - Jsonnet evaluator (same go-jsonnet engine version the
#                      reference links); exposes module `_gojsonnet` with
#                      native_callbacks support. Not an upstream binary.
# Both are pinned by wheel sha256.
set -euo pipefail
req=$(mktemp)
trap 'rm -f "$req"' EXIT
cat > "$req" <<'REQ'
PyYAML==6.0.3 --hash=sha256:ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc
gojsonnet==0.22.0 --hash=sha256:7645cb88583dd2372f6fd6a99be073333c9c66acd18ce7c686ab836d081c613b
REQ
python3 -m pip install --no-cache-dir --disable-pip-version-check --only-binary=:all: --require-hashes -r "$req"
python3 -c "import yaml, _gojsonnet, boto3; print('runtime ok', yaml.__version__)"
