#!/bin/sh
# As the relay (nc -e): connect the task container to the engine address it names on the first line.
if [ "$1" = relay ]; then
    read -r host port
    case "$host" in 10.89.*) ;; *) exit 1 ;; esac
    case "$port" in "" | *[!0-9]*) exit 1 ;; esac
    exec nc "$host" "$port"
fi
# Load the engine images fetched at build time before Floci can reach the daemon, so it never tries to pull one.
sysctl -qw net.ipv4.ip_forward=1
flags="--tls=false --iptables=false --bip=10.89.0.1/24 --default-address-pool=base=10.89.0.0/16,size=24"
dockerd --host=unix:///var/run/docker.sock $flags &
until docker info >/dev/null 2>&1; do sleep 1; done
for image in /engine-images/*.tar; do docker load -i "$image" >/dev/null; done
kill "$!" && wait "$!"
# Engines sit on the daemon's bridge, reachable only from here; floci-venue relays them through this port.
nc -lk -p 4599 -e /usr/local/bin/start-engines.sh relay &
exec dockerd --host=unix:///var/run/docker.sock --host=tcp://127.0.0.1:2375 $flags
