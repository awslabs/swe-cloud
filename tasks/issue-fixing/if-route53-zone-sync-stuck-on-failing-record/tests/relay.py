#!/usr/bin/env python3
"""relay -- the operator's front door to Route 53 in this environment.

Every Route 53 request from the reconciler goes through this relay. It does three
things and nothing else:

  * it forwards the request to the emulator unchanged;
  * it refuses a ChangeResourceRecordSets request whose batch carries more
    changes than the configured per-call limit;
  * it applies the concurrent-writer policy configured for this zone: other
    systems hold leases on some names in this zone and write to them on their
    own schedule, independently of the reconciler's listing.

The reconciler must not try to talk to anything but this relay.

State lives under /run/r53relay/:
  policy.json  written by whoever arms a run; loaded once and removed
  cycle        a plain integer naming the current reconcile cycle, for the log
  calls.jsonl  one JSON line per ChangeResourceRecordSets request
"""

import http.client
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_PORT = 4567
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 4566
RUN_DIR = "/run/r53relay"
POLICY_PATH = os.path.join(RUN_DIR, "policy.json")
CYCLE_PATH = os.path.join(RUN_DIR, "cycle")
LOG_PATH = os.path.join(RUN_DIR, "calls.jsonl")

DEFAULT_LIMIT = 8
NS = "{https://route53.amazonaws.com/doc/2013-04-01/}"

_lock = threading.Lock()
_policy = {"limit": DEFAULT_LIMIT, "seed": None, "armed": False}
_policy_seen = None
_claimed_once = set()


def shuffle_seed(items, seed):
    """A fixed, self-contained shuffle so every process derives the same order."""
    items = list(items)
    state = (seed * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
    for i in range(len(items) - 1, 0, -1):
        state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        j = (state >> 33) % (i + 1)
        items[i], items[j] = items[j], items[i]
    return items


def lease_names(seed):
    """Names this run's other writers hold leases on, derived from the run seed.

    Returns a list of (name, value, holds_every_attempt). A writer that holds
    its lease on every attempt reclaims the name after each of the reconciler's
    calls; one that does not claims the name exactly once.
    """
    names = shuffle_seed(["svc-%02d.example.test." % i for i in range(16)], seed)
    return [
        (names[3], "192.0.2.30", False),
        (names[4], "192.0.2.40", True),
        (names[5], "192.0.2.50", True),
    ]


def _normalise(name):
    name = (name or "").strip().lower()
    return name[:-1] if name.endswith(".") else name


def _claim_for(name):
    parts = _normalise(name).split(".", 1)
    if len(parts) != 2:
        return _normalise(name)
    return "a-%s.%s" % (parts[0], parts[1])


def _load_policy():
    global _policy, _policy_seen
    try:
        raw = open(POLICY_PATH, "rb").read()
    except OSError:
        return
    try:
        os.unlink(POLICY_PATH)
    except OSError:
        pass
    try:
        loaded = json.loads(raw)
    except ValueError:
        return
    with _lock:
        _policy = {
            "limit": int(loaded.get("limit", DEFAULT_LIMIT)),
            "seed": loaded.get("seed"),
            "armed": bool(loaded.get("armed")),
        }
        _policy_seen = raw


def current_policy():
    _load_policy()
    with _lock:
        return dict(_policy)


def current_cycle():
    try:
        return int(open(CYCLE_PATH).read().strip())
    except (OSError, ValueError):
        return 0


def _log(record):
    line = json.dumps(record, sort_keys=True) + "\n"
    with _lock:
        with open(LOG_PATH, "a") as handle:
            handle.write(line)


def _r53():
    import boto3

    return boto3.client(
        "route53",
        endpoint_url="http://%s:%d" % (UPSTREAM_HOST, UPSTREAM_PORT),
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _upsert_a(client, zone_id, name, value):
    client.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": name,
                        "Type": "A",
                        "TTL": 300,
                        "ResourceRecords": [{"Value": value}],
                    },
                }
            ]
        },
    )


def _read_a(client, zone_id, name):
    response = client.list_resource_record_sets(HostedZoneId=zone_id, MaxItems="300")
    for rs in response.get("ResourceRecordSets", []):
        if _normalise(rs["Name"]) == _normalise(name) and rs["Type"] == "A":
            return rs
    return None


def _restore_a(client, zone_id, rs):
    client.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={"Changes": [{"Action": "UPSERT", "ResourceRecordSet": rs}]},
    )


def _delete_a(client, zone_id, rs):
    client.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Changes": [
                {
                    "Action": "DELETE",
                    "ResourceRecordSet": rs,
                }
            ]
        },
    )


def parse_batch(body):
    """Return (zone_id, [ {action,name,type,values} ]) or (None, [])."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None, []
    items = []
    for change in root.iter(NS + "Change"):
        action = change.findtext(NS + "Action") or ""
        rrs = change.find(NS + "ResourceRecordSet")
        if rrs is None:
            continue
        name = rrs.findtext(NS + "Name") or ""
        rtype = rrs.findtext(NS + "Type") or ""
        values = [
            (v.text or "")
            for v in rrs.iter(NS + "Value")
        ]
        items.append(
            {"action": action.strip(), "name": _normalise(name), "type": rtype.strip(), "values": values}
        )
    return None, items


def _error_body(code, message):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ErrorResponse xmlns="https://route53.amazonaws.com/doc/2013-04-01/">'
        "<Error><Type>Sender</Type><Code>%s</Code><Message>%s</Message></Error>"
        "<RequestId>relay-%d</RequestId></ErrorResponse>" % (code, message, int(time.time()))
    )


HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "r53relay/1.0"

    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _reply(self, status, body, content_type="text/xml"):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _forward(self, method, path, body):
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in HOP_HEADERS
        }
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def do_GET(self):
        if self.path == "/_health":
            self._reply(200, "ok", "text/plain")
            return
        body = self._read_body()
        status, payload, _ = self._forward("GET", self.path, body)
        self._reply(status, payload)

    def do_POST(self):
        body = self._read_body()
        path = self.path
        if "/rrset" not in path:
            status, payload, _ = self._forward("POST", path, body)
            self._reply(status, payload)
            return

        _, items = parse_batch(body)
        policy = current_policy()
        limit = policy["limit"]
        cycle = current_cycle()

        if len(items) > limit:
            message = (
                "FATAL problem: the change batch carries %d changes and this "
                "endpoint accepts at most %d changes per ChangeResourceRecordSets "
                "request" % (len(items), limit)
            )
            _log(
                {
                    "cycle": cycle,
                    "changes": len(items),
                    "items": items,
                    "accepted": False,
                    "status": 400,
                    "error": message,
                }
            )
            self._reply(400, _error_body("InvalidChangeBatch", message))
            return

        zone_id = path.split("hostedzone/")[-1].split("/")[0].rstrip("/")
        writers = self._writers_for(policy, items)

        client = None
        priors = {}
        if writers:
            client = _r53()
            for name, value, holds in writers:
                try:
                    priors[name] = _read_a(client, zone_id, name)
                    _upsert_a(client, zone_id, name, value)
                except Exception as exc:  # a failed reclaim is logged, never raised
                    _log({"cycle": cycle, "changes": 0, "items": [], "accepted": False,
                          "status": 0, "error": "reclaim %s failed: %s" % (name, exc)})

        status, payload, _ = self._forward("POST", path, body)
        accepted = 200 <= status < 300

        if client:
            for name, value, holds in writers:
                if not holds:
                    continue
                try:
                    rs = _read_a(client, zone_id, name)
                    if rs is not None and [r["Value"] for r in rs.get("ResourceRecords", [])] == [value]:
                        # put back what the writer displaced; delete only if the name was empty
                        if priors.get(name) is not None:
                            _restore_a(client, zone_id, priors[name])
                        else:
                            _delete_a(client, zone_id, rs)
                except Exception as exc:
                    _log({"cycle": cycle, "changes": 0, "items": [], "accepted": False,
                          "status": 0, "error": "release %s failed: %s" % (name, exc)})

        _log(
            {
                "cycle": cycle,
                "changes": len(items),
                "items": items,
                "accepted": accepted,
                "status": status,
                "error": None if accepted else payload.decode("utf-8", "replace")[:600],
            }
        )
        self._reply(status, payload)

    def _writers_for(self, policy, items):
        """The leases that fire on this request, in the order they were taken."""
        if not policy.get("armed") or not policy.get("seed"):
            return []
        mentioned = set()
        for item in items:
            mentioned.add(item["name"])
        fired = []
        for name, value, holds in lease_names(policy["seed"]):
            plain = _normalise(name)
            if plain not in mentioned and _claim_for(name) not in mentioned:
                continue
            if holds or plain not in _claimed_once:
                _claimed_once.add(plain)
                fired.append((name, value, holds))
        return fired


def main():
    os.makedirs(RUN_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Relay)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
