FROM floci/floci:2.2.0@sha256:e97cd0c1dc2aa14e7697fb5ef5018404c4d6345169dbd276315f0bed2c7b0520 AS floci

FROM python:3.11.12-slim-bookworm@sha256:dbf1de478a55d6763afaa39c2f3d7b54b25230614980276de5cacdde79529d0c
ENV FLOCI_SERVICES_ECS_ENABLED=false
ENV AWS_ENDPOINT_URL=http://localhost:4566 AWS_DEFAULT_REGION=us-east-1 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl procps git zip unzip \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --break-system-packages boto3==1.35.99 pytest==8.3.4 awscli==1.36.40 awscli-local==0.22.2
COPY --from=floci /app/application /opt/floci/floci
ENV FLOCI_VERSION=2.2.0 FLOCI_STORAGE_PERSISTENT_PATH=/opt/floci/data
RUN mkdir -p /opt/floci/data && chmod -R 777 /opt/floci/data
WORKDIR /app
COPY app/ /app/
COPY start_floci.sh /usr/local/bin/start_floci.sh
RUN chmod +x /usr/local/bin/start_floci.sh
# terminus-style harnesses drive the shell through tmux and cannot install it once the network is off.
RUN apt-get update && apt-get install -y --no-install-recommends tmux asciinema && rm -rf /var/lib/apt/lists/*

# Audit experiment: awscli v1 forces real endpoints for sqs/emr/rds/docdb/neptune/health
# unless --endpoint-url is given, so route every bare aws call to AWS_ENDPOINT_URL.
RUN real=$(command -v aws) && mv "$real" /usr/local/bin/aws.real \
 && printf '%s\n' '#!/bin/sh' \
   'case " $* " in *" --endpoint-url"*) exec /usr/local/bin/aws.real "$@";; esac' \
   'if [ -n "$AWS_ENDPOINT_URL" ]; then exec /usr/local/bin/aws.real --endpoint-url "$AWS_ENDPOINT_URL" "$@"; fi' \
   'exec /usr/local/bin/aws.real "$@"' > /usr/local/bin/aws && chmod +x /usr/local/bin/aws
