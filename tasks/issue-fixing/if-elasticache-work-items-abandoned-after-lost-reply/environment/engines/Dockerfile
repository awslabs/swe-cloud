# The engines' private Docker daemon, holding every engine image the task uses so trials need no registry.
FROM docker:27.5.1-dind@sha256:aa3df78ecf320f5fafdce71c659f1629e96e9de0968305fe1de670e0ca9176ce
RUN wget -qO /tmp/crane.tar.gz https://github.com/google/go-containerregistry/releases/download/v0.20.2/go-containerregistry_Linux_x86_64.tar.gz \
    && echo "c14340087103ba9dadf61d45acd20675490fd0ccbd56ac7901fc1b502137f44b  /tmp/crane.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/crane.tar.gz -C /usr/local/bin crane && rm /tmp/crane.tar.gz && mkdir -p /engine-images \
    && crane mutate --platform linux/amd64 valkey/valkey:7.2.5@sha256:57361de39073ea1ef5e7bcfd8068871b21c0a197f01d460e15511f27070fc49b -t valkey/valkey:7.2.5 -o /engine-images/valkey.tar \
    && crane mutate --platform linux/amd64 redpandadata/redpanda:v24.2.18@sha256:0270951e8793c77f0dd9178d137198018fa7ffd920e5a2a3b02a050ec76195b8 -t redpandadata/redpanda:v24.2.18 -o /engine-images/kafka.tar \
    && rm /usr/local/bin/crane
COPY start-engines.sh /usr/local/bin/start-engines.sh
RUN chmod 755 /usr/local/bin/start-engines.sh
ENTRYPOINT ["/usr/local/bin/start-engines.sh"]
