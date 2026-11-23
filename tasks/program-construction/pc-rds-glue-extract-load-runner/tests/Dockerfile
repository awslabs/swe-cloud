# Python, C/C++, Go and Rust with offline libraries for each, so a solution can be written in any of them without network access.
FROM python:3.12-bookworm@sha256:4209fae09a3e20afd863e528bfddc235da5fd5977983e1996cd54cc9a72e6267
ENV AWS_ENDPOINT_URL=http://engines:4566 AWS_DEFAULT_REGION=us-east-1 AWS_REGION=us-east-1 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_EC2_METADATA_DISABLED=true
RUN apt-get update && apt-get install -y --no-install-recommends tmux asciinema build-essential git curl libssl3 iproute2 procps && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --break-system-packages boto3==1.35.99 pytest==8.3.4 pytest-timeout==2.3.1

# C/C++: cmake, and the AWS SDK for C++ built for the services the tasks use.
RUN apt-get update && apt-get install -y --no-install-recommends cmake libcurl4-openssl-dev libssl-dev zlib1g-dev && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 1.11.700 --recurse-submodules --shallow-submodules https://github.com/aws/aws-sdk-cpp /tmp/sdk \
    && cmake -S /tmp/sdk -B /tmp/sdk-build -DCMAKE_BUILD_TYPE=Release -DENABLE_TESTING=OFF -DAUTORUN_UNIT_TESTS=OFF -DCMAKE_INSTALL_PREFIX=/usr/local \
       -DBUILD_ONLY="s3;sqs;sns;dynamodb;kinesis;ssm;secretsmanager;sts;iam;kms;logs;monitoring;eventbridge;route53;glue;firehose;lambda" > /dev/null \
    && cmake --build /tmp/sdk-build -j"$(nproc)" > /dev/null && cmake --install /tmp/sdk-build > /dev/null && ldconfig \
    && rm -rf /tmp/sdk /tmp/sdk-build

# Go: toolchain plus a read-only module proxy. Settings live in go.env because builds run under env -i.
RUN curl -fsSL https://go.dev/dl/go1.25.8.linux-amd64.tar.gz -o /tmp/go.tgz \
    && echo "ceb5e041bbc3893846bd1614d76cb4681c91dadee579426cf21a63f2d7e03be6  /tmp/go.tgz" | sha256sum -c \
    && tar -C /usr/local -xzf /tmp/go.tgz && rm /tmp/go.tgz \
    && ln -s /usr/local/go/bin/go /usr/local/bin/go && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt
COPY toolchain-bundle/go/ /tmp/go-bundle/
RUN cd /tmp/go-bundle && export GOMODCACHE=/tmp/gm GOFLAGS=-modcacherw GOTOOLCHAIN=local && go mod download all \
    && go list -m -f '{{.Path}} {{.Version}}' all > /tmp/selected.txt \
    && python3 -I complete_proxy.py /tmp/gm/cache/download /tmp/selected.txt \
    && mkdir -p /opt/go-proxy && cp -r /tmp/gm/cache/download/. /opt/go-proxy/ && chmod -R a+rX /opt/go-proxy \
    && rm -rf /tmp/gm /tmp/go-bundle /tmp/selected.txt /root/.cache \
    && sed -i 's#^GOPROXY=.*#GOPROXY=file:///opt/go-proxy#; s#^GOSUMDB=.*#GOSUMDB=off#; s#^GOTOOLCHAIN=.*#GOTOOLCHAIN=local#' /usr/local/go/go.env

# Rust: standalone toolchain plus vendored crates. /.cargo/config.toml applies from any working directory.
RUN curl -fsSL https://static.rust-lang.org/dist/rust-1.95.0-x86_64-unknown-linux-gnu.tar.xz -o /tmp/rust.txz \
    && echo "2e0338f18ecbaa4a0f631b9e80e8b8e26bb6fe77dd5454fba8a70cf96c1e84a1  /tmp/rust.txz" | sha256sum -c \
    && mkdir /tmp/rust && tar -C /tmp/rust --strip-components=1 -xJf /tmp/rust.txz \
    && /tmp/rust/install.sh --prefix=/usr/local --components=rustc,cargo,rust-std-x86_64-unknown-linux-gnu --disable-ldconfig > /dev/null \
    && rm -rf /tmp/rust /tmp/rust.txz /usr/local/share/doc/rust
COPY toolchain-bundle/rust/ /tmp/rust-bundle/
RUN cd /tmp/rust-bundle && CARGO_HOME=/tmp/ch cargo vendor --locked /opt/cargo-vendor > /dev/null \
    && rm -rf /tmp/ch /tmp/rust-bundle && chmod -R a+rX /opt/cargo-vendor \
    && mkdir -p /.cargo && printf '[source.crates-io]\nreplace-with = "task"\n[source.task]\ndirectory = "/opt/cargo-vendor"\n[net]\noffline = true\n' > /.cargo/config.toml

COPY floci_venue.py /usr/local/bin/floci-venue
RUN chmod 755 /usr/local/bin/floci-venue
COPY runtime-dependencies.sh /tmp/runtime-dependencies.sh
RUN bash /tmp/runtime-dependencies.sh && rm /tmp/runtime-dependencies.sh
RUN groupadd -g 2000 candidate && useradd -u 2000 -g 2000 -m candidate \
    && mkdir /app && chown candidate:candidate /app \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} +
COPY . /tests/
RUN rm -rf /tests/toolchain-bundle && chmod -R go-rwx /tests
WORKDIR /tests
USER root

COPY toolchains.py /usr/local/lib/task-toolchains.py
RUN python3 -I /usr/local/lib/task-toolchains.py > /etc/task-runtime.json
