"""Task-local helpers: isolated project directories and small comparison utilities."""

import base64
import tomllib
from pathlib import Path

import boto3

import fixture_io
from harness import give_to_candidate, run_command, run_program, write_fixture

ENDPOINT = "http://engines:4566"


class Project:
    """A candidate-owned HOME plus project directory; every invocation gets a clean environment."""

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.dir = root / "proj"
        for path in (self.home, self.dir):
            path.mkdir(mode=0o755)
            give_to_candidate(path)
        self.env = {"HOME": str(self.home), "NO_COLOR": "1"}

    def path(self, name, base=None) -> Path:
        return (base or self.dir) / name

    def write(self, name, text, base=None):
        target = self.path(name, base)
        write_fixture(target, text, mode=0o644)
        return target

    def mkdir(self, name, base=None) -> Path:
        target = self.path(name, base)
        fixture_io.mkdir(target, mode=0o755, parents=True, exist_ok=True)
        current = target
        while current != self.root and current != current.parent:
            give_to_candidate(current)
            current = current.parent
        return target

    def read_text(self, name="fnox.toml", base=None) -> str:
        with fixture_io.open_file(self.path(name, base), "r", encoding="utf-8") as stream:
            return stream.read()

    def read_bytes(self, name="fnox.toml", base=None) -> bytes:
        with fixture_io.open_file(self.path(name, base), "rb") as stream:
            return stream.read()

    def config(self, name="fnox.toml", base=None) -> dict:
        return tomllib.loads(self.read_text(name, base))

    def run(self, *args, stdin=None, cwd=None, env=None, timeout=30):
        return run_program(list(args), cwd=str(cwd or self.dir), stdin=stdin, env={**self.env, **(env or {})}, timeout=timeout)

    def shell(self, script, *, cwd=None, env=None, timeout=30):
        """Run a shell snippet as the candidate (used to evaluate candidate-produced shell text)."""
        return run_command(["bash", "-c", script], cwd=str(cwd or self.dir), env={**self.env, **(env or {})},
                           capture_output=True, text=True, timeout=timeout)


def ok(result):
    assert result.returncode == 0, f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def failed(result):
    assert result.returncode != 0, f"expected failure\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def kms_plaintext(value, key_id) -> bytes:
    """Decrypt an inline config value (base64 ciphertext) with KMS under the named key."""
    return boto3.client("kms").decrypt(CiphertextBlob=base64.b64decode(value, validate=True), KeyId=key_id)["Plaintext"]


def kms_ciphertext(plaintext, key_id) -> str:
    blob = boto3.client("kms").encrypt(KeyId=key_id, Plaintext=plaintext.encode() if isinstance(plaintext, str) else plaintext)
    return base64.b64encode(blob["CiphertextBlob"]).decode()


def parameter(name):
    """(Type, Value) of an SSM parameter, or None when absent."""
    try:
        p = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]
    except boto3.client("ssm").exceptions.ParameterNotFound:
        return None
    return p["Type"], p["Value"]


def secret_string(name):
    sm = boto3.client("secretsmanager")
    try:
        return sm.get_secret_value(SecretId=name)["SecretString"]
    except sm.exceptions.ResourceNotFoundException:
        return None


def env_lines(text):
    """Parse `export -f env` output for simple values: KEY=VALUE with optional surrounding quotes."""
    result = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        assert key not in result, f"duplicate key {key}"
        result[key] = value
    return result


def child_env(project, *args, names, flags=(), env=None, stdin=None):
    """Run `[args] exec [flags]` and return the child's values for `names` (absent names are omitted)."""
    script = 'for n in "$@"; do if [ -n "${!n+x}" ]; then printf "%s\\0%s\\0" "$n" "${!n}"; fi; done'
    result = project.run(*args, "exec", *flags, "--", "bash", "-c", script, "probe", *names, env=env, stdin=stdin)
    ok(result)
    parts = result.stdout.split("\0")
    assert parts[-1] == "", result.stdout
    pairs = parts[:-1]
    return dict(zip(pairs[0::2], pairs[1::2], strict=True)), result


def is_number(value, expected):
    return type(value) in (int, float) and value == expected

