#!/bin/bash
# Harbor reads reward.json before reward.txt; only this script may write either. Remove just those two:
# Harbor writes this script's output to /logs/verifier/test-stdout.txt, which must stay.
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
# Reward 1 only when nothing outside the sandbox answers: HTTPS, external DNS resolution,
# UDP/53 to an external IP, ICMP, and the instance-metadata endpoint 169.254.169.254.
mkdir -p /logs/verifier
if python3 - <<'PY'
import socket, struct, sys, urllib.error, urllib.request

reached = []

def report(check, ok, detail):
    print(("reached " if ok else "blocked ") + check, detail)
    if ok:
        reached.append(check)

for url in ("https://pypi.org/simple/", "https://github.com", "https://1.1.1.1"):
    try:
        urllib.request.urlopen(url, timeout=10)
        report("https " + url, True, "")
    except urllib.error.HTTPError as exc:
        # An HTTP error status is still an answer from outside the sandbox.
        report("https " + url, True, f"HTTP {exc.code}")
    except Exception as exc:
        report("https " + url, False, type(exc).__name__)

# The resolver in /etc/resolv.conf (Docker's embedded DNS forwards to the host's resolver).
for name in ("pypi.org", "github.com"):
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(name, 443)}
        report("dns " + name, True, sorted(addrs))
    except Exception as exc:
        report("dns " + name, False, type(exc).__name__)

# A DNS query for example.com straight to public resolvers over UDP/53.
query = struct.pack(">HHHHHH", 0x5343, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
for server in ("1.1.1.1", "8.8.8.8"):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(5)
            s.sendto(query, (server, 53))
            s.recvfrom(512)
        report("udp/53 " + server, True, "")
    except Exception as exc:
        report("udp/53 " + server, False, type(exc).__name__)

# ICMP echo, through an unprivileged ICMP socket or, failing that, a raw one.
def checksum(data):
    total = sum(struct.unpack(f">{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    return ~(total + (total >> 16)) & 0xFFFF

for server in ("1.1.1.1", "8.8.8.8"):
    payload = b"swe-cloud-egress"
    header = struct.pack(">BBHHH", 8, 0, 0, 0x5343, 1)
    packet = struct.pack(">BBHHH", 8, 0, checksum(header + payload), 0x5343, 1) + payload
    try:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        except OSError:
            s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        with s:
            s.settimeout(5)
            s.sendto(packet, (server, 0))
            s.recvfrom(1024)
        report("icmp " + server, True, "")
    except Exception as exc:
        report("icmp " + server, False, type(exc).__name__)

# Instance metadata; on a cloud host this would expose the host's credentials.
for url in ("http://169.254.169.254/latest/meta-data/",):
    try:
        urllib.request.urlopen(url, timeout=5)
        report("imds " + url, True, "")
    except urllib.error.HTTPError as exc:
        report("imds " + url, True, f"HTTP {exc.code}")
    except Exception as exc:
        report("imds " + url, False, type(exc).__name__)

print("NOT sealed: " + ", ".join(reached) if reached else "sealed")
sys.exit(1 if reached else 0)
PY
then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
