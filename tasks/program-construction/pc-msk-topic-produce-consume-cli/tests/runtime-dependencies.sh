#!/bin/bash
# Runtime setup for a fresh python:3.12-bookworm image (solver, verifier and
# probe images). The reference payload itself is a static-librdkafka ELF that
# needs only glibc, so nothing here is required to run it. What is installed:
#  - confluent-kafka (pinned; bundles librdkafka 2.6.1, the same librdkafka
#    release the reference is linked with): general-purpose Kafka client used
#    by fixtures/verifiers (topic creation, read-back, group offsets) and
#    available to a reconstruction.
#  - librdkafka-dev / libyajl-dev from Debian bookworm: general-purpose C Kafka
#    client and JSON libraries for a native reconstruction. No kcat/kafkacat
#    package or alias is installed.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends librdkafka-dev libyajl-dev pkg-config
rm -rf /var/lib/apt/lists/*
python3 -m pip install --no-cache-dir "confluent-kafka==2.6.1"
python3 -c 'import confluent_kafka; print(confluent_kafka.version(), confluent_kafka.libversion())'
