# main: a plain Python image with the venue helper; Floci and the engines are the other services.
FROM python:3.11.12-slim-bookworm@sha256:dbf1de478a55d6763afaa39c2f3d7b54b25230614980276de5cacdde79529d0c
ENV AWS_ENDPOINT_URL=http://engines:4566 AWS_DEFAULT_REGION=us-east-1 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
ENV PYTHONPATH=/app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl procps iproute2 git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir boto3==1.35.99 pytest==8.3.4 pika==1.3.2
COPY floci_venue.py /usr/local/bin/floci-venue
RUN chmod +x /usr/local/bin/floci-venue
COPY cloud/vm-destroy /usr/local/bin/vm-destroy
COPY cloud/sandbox-cloud /usr/local/bin/sandbox-cloud
RUN chmod +x /usr/local/bin/vm-destroy /usr/local/bin/sandbox-cloud
WORKDIR /app
COPY app/ /app/
# terminus-style harnesses drive the shell through tmux and cannot install it once the network is off.
RUN apt-get update && apt-get install -y --no-install-recommends tmux asciinema && rm -rf /var/lib/apt/lists/*
