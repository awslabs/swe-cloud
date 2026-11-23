FROM python:3.11.12-slim-bookworm@sha256:dbf1de478a55d6763afaa39c2f3d7b54b25230614980276de5cacdde79529d0c
ENV AWS_ENDPOINT_URL=http://engines:4566 AWS_DEFAULT_REGION=us-east-1 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl procps iproute2 git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir boto3==1.35.99 pytest==8.3.4 "psycopg[binary]==3.2.3"
COPY floci_venue.py /usr/local/bin/floci-venue
RUN chmod +x /usr/local/bin/floci-venue
WORKDIR /app
COPY app/ /app/
# terminus-style harnesses drive the shell through tmux and cannot install it once the network is off.
RUN apt-get update && apt-get install -y --no-install-recommends tmux asciinema && rm -rf /var/lib/apt/lists/*
