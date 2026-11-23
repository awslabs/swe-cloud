#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). The reference itself is a static ELF
# and needs no system libraries. These pinned general-purpose clients are supporting
# infrastructure for fixtures and observation, not part of the reconstructed behaviour:
#   confluent-kafka: create topics, produce records, commit consumer-group offsets on MSK
#   requests:        read the service's HTTP JSON API
set -euo pipefail
python3 -m pip install --no-cache-dir 'confluent-kafka==2.16.0' 'requests==2.34.2'
python3 -c 'import confluent_kafka, requests; print("confluent-kafka", confluent_kafka.version(), confluent_kafka.libversion(), "requests", requests.__version__)'
