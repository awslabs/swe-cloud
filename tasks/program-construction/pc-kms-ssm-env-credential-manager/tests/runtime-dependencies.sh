#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3), shared by the reference,
# solver and verifier images.
#
# The reference payload is a fully static x86-64 ELF: it needs no shared
# libraries, no CA bundle (the venue endpoint is plain http://engines:4566) and
# no other runtime assets. Nothing upstream is installed here.
#
# Supporting infrastructure for reconstruction: the configuration file is TOML,
# a public standard format whose parsing/round-trip editing is not the intended
# task work. Python 3.12 ships a TOML reader (tomllib) but no writer, so a
# pinned general-purpose, formatting-preserving TOML library is provided.
set -euo pipefail

python3 -m pip install --no-cache-dir 'tomlkit==0.13.3'
python3 -c 'import tomlkit, boto3; print("tomlkit", tomlkit.__version__, "boto3", boto3.__version__)'
