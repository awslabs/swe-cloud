"""Helpers shared by the test modules: Route 53 zone fixtures, the project directory and output parsing."""

import re
import uuid
from collections import Counter
from pathlib import Path

import boto3
from harness import run_program, write_fixture

HEADER = 'var REG_NONE = NewRegistrar("none");\nvar R53 = NewDnsProvider("r53");\n'
CREDS = '{ "r53": { "TYPE": "ROUTE53", "KeyId": "test", "SecretKey": "test" } }\n'

_route53 = None


def route53():
    global _route53
    if _route53 is None:
        _route53 = boto3.client("route53")
    return _route53


def unique_tag():
    return uuid.uuid4().hex[:10]


def rr(ttl, *values):
    """Expected ordinary record set: TTL plus the multiset of stored values."""
    return ("rr", ttl, tuple(sorted(values)))


def alias(target, zone_id, evaluate):
    """Expected alias record set: DNSName, HostedZoneId, EvaluateTargetHealth (a bool)."""
    assert type(evaluate) is bool
    return ("alias", target, zone_id, evaluate)


def dotted(name):
    """A stored name with or without its final dot denotes the same name."""
    return name if name.endswith(".") else name + "."


class Zone:
    def __init__(self, prefix, name=None):
        self.name = name or f"{prefix}-{uuid.uuid4().hex[:10]}.example"
        created = route53().create_hosted_zone(Name=self.name, CallerReference=f"tests-{self.name}")
        self.id = created["HostedZone"]["Id"].split("/")[-1]
        # The apex NS/SOA sets Route 53 creates with every zone.
        self.initial = self.state()

    def fqdn(self, label):
        return self.name + "." if label == "@" else f"{label}.{self.name}."

    def rrsets(self):
        sets = []
        for page in route53().get_paginator("list_resource_record_sets").paginate(HostedZoneId=self.id):
            sets += page["ResourceRecordSets"]
        return sets

    def state(self):
        """Complete zone content keyed by (relative name, type); values are order-insensitive."""
        state = {}
        suffix = "." + self.name + "."
        for rrset in self.rrsets():
            name = dotted(rrset["Name"])
            label = "@" if name == self.name + "." else name[: -len(suffix)] if name.endswith(suffix) else name
            key = (label, rrset["Type"])
            assert key not in state, f"duplicate record set {key}"
            if "AliasTarget" in rrset:
                target = rrset["AliasTarget"]
                state[key] = ("alias", dotted(target["DNSName"]), target["HostedZoneId"], target["EvaluateTargetHealth"])
            else:
                state[key] = ("rr", rrset.get("TTL"), tuple(sorted(r["Value"] for r in rrset["ResourceRecords"])))
        return state

    def apex(self, ns_ttl=300):
        """The initial apex SOA/NS sets, with the NS TTL the program is expected to leave."""
        ns = self.initial[("@", "NS")]
        return {("@", "SOA"): self.initial[("@", "SOA")], ("@", "NS"): ("rr", ns_ttl, ns[2])}

    def seed(self, label, rtype, ttl, *values):
        self._change({"Name": self.fqdn(label), "Type": rtype, "TTL": ttl, "ResourceRecords": [{"Value": v} for v in values]})

    def seed_alias(self, label, rtype, target, zone_id, evaluate):
        self._change({"Name": self.fqdn(label), "Type": rtype, "AliasTarget": {"HostedZoneId": zone_id, "DNSName": target, "EvaluateTargetHealth": evaluate}})

    def _change(self, rrset):
        route53().change_resource_record_sets(HostedZoneId=self.id, ChangeBatch={"Changes": [{"Action": "UPSERT", "ResourceRecordSet": rrset}]})

    def delete(self):
        apex = self.name + "."
        changes = [{"Action": "DELETE", "ResourceRecordSet": s} for s in self.rrsets() if not (s["Name"] == apex and s["Type"] in ("SOA", "NS"))]
        if changes:
            route53().change_resource_record_sets(HostedZoneId=self.id, ChangeBatch={"Changes": changes})
        route53().delete_hosted_zone(Id=self.id)


class Project:
    """A candidate-owned working directory holding dnsconfig.js and creds.json."""

    def __init__(self, root: Path):
        self.root = root
        self.write("creds.json", CREDS)

    def write(self, relative, text):
        write_fixture(self.root / relative, text, mode=0o644)
        return self.root / relative

    def config(self, body, name="dnsconfig.js"):
        """Write a configuration: the standard registrar/provider header followed by body."""
        return self.write(name, HEADER + body)

    def run(self, *args, cwd=None, env=None):
        return run_program(["--no-colors", *args], cwd=cwd or self.root, env=env)


SUMMARY = re.compile(r"^Done\. (\d+) corrections\.$", re.M)
HEADER_LINE = re.compile(r"^\*{20} Domain: (\S+)$", re.M)


def corrections(result):
    """The total from the single `Done. N corrections.` summary line."""
    found = SUMMARY.findall(result.stdout)
    assert len(found) == 1, f"expected one summary line, got {found!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return int(found[0])


def zone_headers(result):
    return Counter(HEADER_LINE.findall(result.stdout))


def ok(result, count=None):
    """Assert a zero exit status and, optionally, the reported correction total."""
    assert result.returncode == 0, f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    if count is not None:
        assert corrections(result) == count, result.stdout
    return result


def failed(result):
    assert result.returncode != 0, f"expected failure\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result
