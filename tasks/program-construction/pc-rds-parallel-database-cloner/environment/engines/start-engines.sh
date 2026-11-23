#!/bin/sh
# Load the engine images fetched at build time before Floci can reach the daemon, so it never tries to pull one.
sysctl -qw net.ipv4.ip_forward=1
flags="--tls=false --iptables=false --bip=10.89.0.1/24 --default-address-pool=base=10.89.0.0/16,size=24"
dockerd --host=unix:///var/run/docker.sock $flags &
until docker info >/dev/null 2>&1; do sleep 1; done
for image in /engine-images/*.tar; do docker load -i "$image" >/dev/null; done
kill "$!" && wait "$!"
exec dockerd --host=unix:///var/run/docker.sock --host=tcp://127.0.0.1:2375 $flags
