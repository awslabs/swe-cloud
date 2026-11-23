"""Shared fixtures: a fresh vault home per test and owned IAM users/roles in the venue."""

import configparser
import datetime as dt
import json
import re
import time
import uuid
from types import SimpleNamespace

import boto3
import pytest
from harness import VENUE, give_to_candidate, run_command, run_program, workspace, write_fixture

ACCOUNT = "000000000000"
PASSPHRASE = "vault-test-passphrase"
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
# The invocation environment run_program supplies, less the venue's dummy credentials.
VENUE_CHILD = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", **{k: v for k, v in VENUE.items() if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")}}
DUMP_ENV = ["python3", "-c", "import json, os; print(json.dumps(dict(os.environ)))"]


def unique(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Aws:
    """IAM users and roles owned by one test; everything is deleted at teardown."""

    def __init__(self):
        self.iam = boto3.client("iam")
        self.users = []
        self.roles = []

    def user(self):
        name = unique("vault-user")
        arn = self.iam.create_user(UserName=name)["User"]["Arn"]
        self.users.append(name)
        key = self.iam.create_access_key(UserName=name)["AccessKey"]
        return SimpleNamespace(name=name, arn=arn, key_id=key["AccessKeyId"], secret=key["SecretAccessKey"])

    def role(self):
        name = unique("vault-role")
        policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}, "Action": "sts:AssumeRole"}]}
        arn = self.iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(policy))["Role"]["Arn"]
        self.roles.append(name)
        return SimpleNamespace(name=name, arn=arn)

    def keys(self, user):
        return sorted(k["AccessKeyId"] for k in self.iam.list_access_keys(UserName=user.name)["AccessKeyMetadata"])

    def cleanup(self):
        for name in self.users:
            try:
                for key in self.iam.list_access_keys(UserName=name)["AccessKeyMetadata"]:
                    self.iam.delete_access_key(UserName=name, AccessKeyId=key["AccessKeyId"])
                self.iam.delete_user(UserName=name)
            except Exception as error:  # noqa: BLE001 -- cleanup must continue
                print(f"cleanup of user {name} failed: {error}")
        for name in self.roles:
            try:
                self.iam.delete_role(RoleName=name)
            except Exception as error:  # noqa: BLE001
                print(f"cleanup of role {name} failed: {error}")


@pytest.fixture
def aws():
    owner = Aws()
    try:
        yield owner
    finally:
        owner.cleanup()


class Vault:
    """A candidate-owned HOME with an AWS config file and a file-backend store."""

    def __init__(self, root):
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        give_to_candidate(self.home)
        self.config_path = self.home / ".aws" / "config"

    def write_config(self, text):
        write_fixture(self.config_path, text)

    def env(self, extra=None):
        return {"HOME": str(self.home), "AWS_VAULT_BACKEND": "file", "AWS_VAULT_FILE_PASSPHRASE": PASSPHRASE, **(extra or {})}

    def run(self, *args, env=None, stdin=None, timeout=45):
        result = run_program(list(args), cwd=self.root, stdin=stdin, env=self.env(env), timeout=timeout)
        print(f"$ program {' '.join(args)} -> {result.returncode}\n{result.stdout}\n[stderr]\n{result.stderr}")
        return result

    def ok(self, *args, **options):
        result = self.run(*args, **options)
        assert result.returncode == 0, result.stderr
        return result

    def add(self, profile, user, *flags, env=None):
        result = self.ok("add", "--env", *flags, profile, env={"AWS_ACCESS_KEY_ID": user.key_id, "AWS_SECRET_ACCESS_KEY": user.secret, **(env or {})})
        return result

    def export(self, profile, *flags, env=None):
        before = time.time()
        result = self.ok("export", "--format=json", *flags, profile, env=env)
        creds = parse_json_creds(result.stdout)
        creds["_window"] = (before, time.time())
        return creds

    def exec_env(self, profile, *flags, env=None):
        before = time.time()
        result = self.ok("exec", *flags, profile, "--", *DUMP_ENV, env=env)
        child = json.loads(result.stdout)
        child["_window"] = (before, time.time())
        return child

    def sessions(self):
        return parse_sessions(self.ok("list", "--sessions").stdout)

    def credentials(self):
        return self.ok("list", "--credentials").stdout.splitlines()

    def read_as_candidate(self, path):
        """Candidate-written files are read with the candidate's identity, never as root."""
        result = run_command(["cat", "--", str(path)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout


@pytest.fixture
def vault():
    with workspace() as root:
        yield Vault(root)


# ---- parsing and comparison helpers ---------------------------------------------------


def parse_json_creds(stdout):
    value = json.loads(stdout)
    assert isinstance(value, dict), stdout
    version = value.get("Version")
    # Value equivalence for the number; a boolean is not a number here.
    assert type(version) in (int, float) and version == 1, value
    for name in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration", "AccountId"):
        if name in value:
            assert isinstance(value[name], str) and value[name], (name, value)
    return value


def creds_of(value):
    """The credential triple of an exported/child credential set, keyed uniformly."""
    keys = {"AccessKeyId": "key", "SecretAccessKey": "secret", "SessionToken": "token",
            "AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "secret", "AWS_SESSION_TOKEN": "token",
            "aws_access_key_id": "key", "aws_secret_access_key": "secret", "aws_session_token": "token"}
    return {short: value.get(name) for name, short in keys.items() if name in value}


def identity(value):
    c = creds_of(value)
    sts = boto3.client("sts", aws_access_key_id=c["key"], aws_secret_access_key=c["secret"], aws_session_token=c.get("token"))
    return sts.get_caller_identity()["Arn"]


def expiry(text):
    assert TIMESTAMP.fullmatch(text), f"expiration {text!r} is not YYYY-MM-DDTHH:MM:SSZ"
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()


def assert_lifetime(text, seconds, window, slack=3):
    """The expiration lies `seconds` after the invocation window."""
    when = expiry(text)
    before, after = window
    assert before + seconds - slack <= when <= after + seconds + slack, (text, seconds, when - before)


def parse_env(stdout, prefix=""):
    pairs = {}
    for line in stdout.splitlines():
        assert line.startswith(prefix), line
        name, sep, value = line[len(prefix):].partition("=")
        assert sep and re.fullmatch(r"[A-Z_]+", name), line
        assert name not in pairs, f"duplicate {name}"
        pairs[name] = value
    return pairs


def parse_ini(stdout):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(stdout)
    sections = parser.sections()
    assert len(sections) == 1, sections
    return sections[0], dict(parser[sections[0]])


_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def parse_sessions(stdout):
    """`list --sessions` lines as (type, remaining seconds); order is not graded."""
    sessions = []
    for line in stdout.splitlines():
        kind, sep, ttl = line.partition(":")
        match = _DURATION.fullmatch(ttl)
        assert sep and match and ttl, line
        hours, minutes, secs = (int(part or 0) for part in match.groups())
        sessions.append((kind, hours * 3600 + minutes * 60 + secs))
    return sorted(sessions)


def assert_sessions(observed, expected, slack=8):
    """Compare session multisets: same types, remaining lifetimes within slack seconds."""
    assert len(observed) == len(expected), (observed, expected)
    remaining = list(observed)
    for kind, ttl in sorted(expected):
        match = next((s for s in remaining if s[0] == kind and abs(s[1] - ttl) <= slack), None)
        assert match is not None, (observed, expected)
        remaining.remove(match)


def parse_table(stdout):
    """`list` rows as cell lists; every row's columns must line up with the header."""
    lines = stdout.splitlines()
    starts = [m.start() for m in re.finditer(r"\S+", lines[0])]
    assert len(starts) == 3, lines[0]
    rows = []
    for line in lines:
        cells = list(re.finditer(r"\S+", line))
        assert [m.start() for m in cells] == starts, f"misaligned row {line!r}"
        rows.append([m.group() for m in cells])
    assert rows[:2] == [["Profile", "Credentials", "Sessions"], ["=======", "===========", "========"]], rows[:2]
    return rows[2:]
