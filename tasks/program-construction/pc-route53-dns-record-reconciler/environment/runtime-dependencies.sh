#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3), shared by reference, solver
# and verifier images.
#
# The reference itself is a static Go ELF (CGO disabled) and needs only
# ca-certificates and tzdata, which the base image already ships.
#
# Supporting infrastructure for reconstruction: the task input (dnsconfig.js)
# is ordinary JavaScript. Executing standard JavaScript is not the task; the
# DNS DSL semantics are. So a pinned general-purpose JavaScript runtime
# (Node.js LTS, official checksum-verified tarball) is installed as `node`.
# Nothing from the upstream project is installed.
set -euo pipefail

NODE_VERSION=v24.22.1
NODE_SHA256=eb6728e77c576a35162c122c45ccadf224d89ff00abc8dae46cc2897c6002d7c

tmp=$(mktemp -d)
curl -fsSL --retry 5 -o "$tmp/node.tar.gz" \
  "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.gz"
echo "${NODE_SHA256}  $tmp/node.tar.gz" | sha256sum -c -
rm -rf /opt/node && mkdir -p /opt/node
tar -C /opt/node --strip-components=1 -xzf "$tmp/node.tar.gz"
ln -sf /opt/node/bin/node /usr/local/bin/node
rm -rf "$tmp"

python3 -c 'import boto3' >/dev/null
test -d /usr/share/zoneinfo
test -f /etc/ssl/certs/ca-certificates.crt
node --version
