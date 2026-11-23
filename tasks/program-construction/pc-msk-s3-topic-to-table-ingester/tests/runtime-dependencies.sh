#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+ boto3), used for probe, solver and verifier images.
#
# The reference ELF needs only standard Debian runtime libraries already in the base image
# (glibc, libgcc_s, libssl3/libcrypto3, zlib) plus CA certificates.
#
# Admitted general-purpose infrastructure (pinned):
#  * confluent-kafka: Kafka client (bundled librdkafka) used to create topics and produce
#    fixture messages against the MSK broker Floci starts.
#  * pyarrow: Parquet reader/writer. Parquet encoding/decoding is supporting
#    infrastructure for this task (data files and Delta checkpoints are Parquet); the
#    Delta log protocol itself is not provided by a library here.
#  * jmespath: general-purpose JMESPath query evaluator. The transform flag uses JMESPath
#    expressions; the standard language is infrastructure, while the program-specific
#    functions and Kafka metadata sources remain to be reconstructed.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates libssl3 zlib1g
rm -rf /var/lib/apt/lists/*
python3 -m pip install --no-cache-dir \
    confluent-kafka==2.16.0 \
    pyarrow==26.0.0 \
    jmespath==1.1.0
python3 -c 'import confluent_kafka, pyarrow, boto3, jmespath; print("confluent-kafka", confluent_kafka.version(), confluent_kafka.libversion(), "pyarrow", pyarrow.__version__, "boto3", boto3.__version__)'
