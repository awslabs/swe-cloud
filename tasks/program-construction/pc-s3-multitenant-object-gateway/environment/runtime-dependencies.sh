#!/usr/bin/env bash
# Runtime setup for the python:3.12-bookworm solver/verifier image.
#
# The reference is a statically linked (CGO_ENABLED=0) Go ELF: it needs no
# shared libraries, no data assets and no CA bundle (the venue is plain HTTP).
# The admitted wire formats (S3 REST/XML, SigV4, the gateway's JSON account
# and ACL records) are covered by the Python standard library (http.server,
# hmac/hashlib, xml.etree, json) plus the preinstalled boto3/botocore, so no
# additional format reader is installed.  Nothing from upstream is installed.
set -euo pipefail
python3 -c 'import boto3, botocore, xml.etree.ElementTree, http.server, hmac, hashlib, json; print("boto3", boto3.__version__)'
