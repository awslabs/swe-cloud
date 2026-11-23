#!/usr/bin/env bash
# Runtime setup for the python:3.12-bookworm solver/verifier images (boto3 preinstalled).
# The reference itself is a fully static x86-64 ELF: no shared libraries, no data assets.
#
# Admitted supporting infrastructure: a general-purpose Jsonnet evaluator. The program's config
# language is Jsonnet (with --ext-str variables); evaluating Jsonnet is not the behaviour under
# reconstruction, so the official Jsonnet Python binding (C++ jsonnet 0.21.0, module `_jsonnet`)
# is installed, hash-pinned, for reconstruction and verification code. Everything else the
# program reads/writes is JSON (Python stdlib).
set -euo pipefail
req="$(mktemp)"
cat >"$req" <<'REQ'
jsonnet==0.21.0 --hash=sha256:fccebb019917004cf860490a80d17189bad01c9d425b7a1cb138a14745488cf0
REQ
python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r "$req"
rm -f "$req"
python3 -c 'import _jsonnet, boto3, json; json.loads(_jsonnet.evaluate_snippet("t", "{a: std.extVar(\"x\")}", ext_vars={"x": "1"})); print("jsonnet", _jsonnet.version, "boto3", boto3.__version__)'
