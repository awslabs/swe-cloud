# The engines' private Docker daemon, holding every engine image the task uses so trials need no registry.
FROM docker:27.5.1-dind@sha256:aa3df78ecf320f5fafdce71c659f1629e96e9de0968305fe1de670e0ca9176ce
RUN wget -qO /tmp/crane.tar.gz https://github.com/google/go-containerregistry/releases/download/v0.20.2/go-containerregistry_Linux_x86_64.tar.gz \
    && echo "c14340087103ba9dadf61d45acd20675490fd0ccbd56ac7901fc1b502137f44b  /tmp/crane.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/crane.tar.gz -C /usr/local/bin crane && rm /tmp/crane.tar.gz && mkdir -p /engine-images \
    && crane mutate --platform linux/amd64 mysql:8.0.35@sha256:c6812f0dcd978b589d9d3b90c20bd5e4a799deab4fd492985e0a076c5be1ee40 -t mysql:8.0.35 -o /engine-images/mysql.tar \
    && rm /usr/local/bin/crane
COPY start-engines.sh /usr/local/bin/start-engines.sh
RUN chmod 755 /usr/local/bin/start-engines.sh
ENTRYPOINT ["/usr/local/bin/start-engines.sh"]
