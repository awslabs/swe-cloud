#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (solver, verifier and probe images).
#
# * Shared libraries the gearmand ELF links against (Debian bookworm sonames):
#   MariaDB Connector/C (libmariadb.so.3, incl. caching_sha2_password plugin for
#   MySQL 8 servers), libevent 2.1, Boost.Program_options 1.74.  libstdc++,
#   OpenSSL 3 and zlib are part of the base image.
# * PyMySQL (+cryptography for MySQL 8 caching_sha2/sha256 auth): a pinned
#   general-purpose MySQL client so tests and solvers can inspect the queue
#   table.  No Gearman client/server library is installed: the Gearman wire
#   protocol is the subject of the task.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  libmariadb3 \
  libevent-2.1-7 \
  libevent-pthreads-2.1-7 \
  libboost-program-options1.74.0
rm -rf /var/lib/apt/lists/*

python3 -m pip install --no-cache-dir \
  "PyMySQL==1.1.1" \
  "cryptography==43.0.3" \
  "cffi==1.17.1" \
  "pycparser==2.22"
