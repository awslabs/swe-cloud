"""Invocation, config discovery and serialization of resolved secrets (export / env --json / get / list / exec)."""

import json
import os
import tomllib
from datetime import datetime

from support import child_env, env_lines, failed, is_number, kms_ciphertext, kms_plaintext, ok

DEFAULTS = """\
[secrets]
PLAIN = { default = "hello world" }
TRICKY = { default = "it's \\"q\\" $HOME \\\\ end" }
MULTI = { default = "line1\\nline2" }
EMPTY = { default = "" }
NOENV = { default = "hidden", env = false }
EXONLY = { default = "exec-only", env = "exec" }
DESCR = { default = "d", description = "Described one" }
"""

TRICKY = "it's \"q\" $HOME \\ end"


def kms_project(project, key, extra=""):
    project.write("fnox.toml", f"""\
root = true
[providers.kms]
type = "aws-kms"
key_id = "{key}"
region = "us-east-1"

{DEFAULTS}KTOK = {{ provider = "kms", value = "{kms_ciphertext("kms value", key)}" }}
{extra}""")


EXPORTED = {"PLAIN": "hello world", "TRICKY": TRICKY, "MULTI": "line1\nline2", "EMPTY": "", "DESCR": "d", "KTOK": "kms value"}


def test_get_prints_value_and_newline(project, kms_key):
    kms_project(project, kms_key)
    assert ok(project.run("get", "KTOK")).stdout == "kms value\n"
    assert ok(project.run("get", "MULTI")).stdout == "line1\nline2\n"
    assert ok(project.run("get", "EMPTY")).stdout == "\n"
    # env = false only keeps a secret out of environments; get still resolves it.
    assert ok(project.run("get", "NOENV")).stdout == "hidden\n"
    undeclared = failed(project.run("get", "UNDECLARED"))
    assert undeclared.stdout == ""


def test_export_json_document(project, kms_key):
    kms_project(project, kms_key)
    doc = json.loads(ok(project.run("export", "--format", "json")).stdout)
    assert set(doc) == {"secrets", "metadata"}
    assert doc["secrets"] == EXPORTED
    meta = doc["metadata"]
    assert set(meta) == {"profile", "exported_at", "total_secrets"}
    assert meta["profile"] == "default"
    assert is_number(meta["total_secrets"], 6)
    datetime.fromisoformat(meta["exported_at"])


def test_export_all_and_named_keys(project, kms_key):
    kms_project(project, kms_key)
    doc = json.loads(ok(project.run("export", "--all", "-f", "json")).stdout)
    assert doc["secrets"] == {**EXPORTED, "NOENV": "hidden", "EXONLY": "exec-only"}
    assert is_number(doc["metadata"]["total_secrets"], 8)
    # Named keys select exactly those secrets, including env = false ones.
    named = json.loads(ok(project.run("export", "-f", "json", "PLAIN", "NOENV")).stdout)
    assert named["secrets"] == {"PLAIN": "hello world", "NOENV": "hidden"}
    failed(project.run("export", "-f", "json", "PLAIN", "NOT_DECLARED"))


def test_export_toml_document(project, kms_key):
    kms_project(project, kms_key)
    doc = tomllib.loads(ok(project.run("export", "-f", "toml")).stdout)
    assert doc["secrets"] == EXPORTED
    assert doc["metadata"]["profile"] == "default"
    assert is_number(doc["metadata"]["total_secrets"], 6)


def test_export_shell_format_evaluates_to_values(project, kms_key):
    kms_project(project, kms_key)
    out = ok(project.run("export", "-f", "shell")).stdout
    project.write("exported.sh", out)
    script = ('env -i bash -c \'. ./exported.sh; for n in PLAIN TRICKY MULTI EMPTY DESCR KTOK NOENV EXONLY; do '
              'if [ -n "${!n+x}" ]; then printf "%s\\0%s\\0" "$n" "${!n}"; fi; done; '
              'export -p | grep -c "^declare -x \\(PLAIN\\|KTOK\\)=" >&2\'')
    result = project.shell(script)
    assert result.returncode == 0, result.stderr
    parts = result.stdout.split("\0")[:-1]
    assert dict(zip(parts[0::2], parts[1::2], strict=True)) == EXPORTED
    assert result.stderr.strip() == "2"  # the shell format exports the variables


def test_export_env_format_and_header(project, kms_key):
    # Values here need no escaping; quoting of special characters in dotenv text is not graded.
    project.write("fnox.toml", f"""\
root = true
[providers.kms]
type = "aws-kms"
key_id = "{kms_key}"
region = "us-east-1"

[secrets]
PLAIN = {{ default = "hello world" }}
EMPTY = {{ default = "" }}
DESCR = {{ default = "d", description = "Described one" }}
NOENV = {{ default = "hidden", env = false }}
EXONLY = {{ default = "exec-only", env = "exec" }}
KTOK = {{ provider = "kms", value = "{kms_ciphertext("kms value", kms_key)}" }}
""")
    out = ok(project.run("export")).stdout
    assert env_lines(out) == {"PLAIN": "hello world", "EMPTY": "", "DESCR": "d", "KTOK": "kms value"}
    header = ok(project.run("export", "--header")).stdout
    comments = [line for line in header.splitlines() if line.startswith("#")]
    assert comments and any("default" in line for line in comments)
    assert env_lines(header) == env_lines(out)
    yaml = ok(project.run("export", "-f", "yaml")).stdout.splitlines()
    assert yaml[0] == "secrets:"
    body = {}
    for line in yaml[1:]:
        if not line.startswith("  "):
            break
        key, _, value = line.strip().partition(": ")
        body[key.rstrip(":")] = value.strip("'\"")
    assert body == {"PLAIN": "hello world", "EMPTY": "", "DESCR": "d", "KTOK": "kms value"}


def test_export_to_output_file(project, kms_key):
    kms_project(project, kms_key)
    result = ok(project.run("export", "-f", "json", "-o", "out.json"))
    assert "kms value" not in result.stdout
    assert json.loads(project.read_text("out.json"))["secrets"] == EXPORTED


def test_env_json_describes_exec_and_shell_environments(project, kms_key):
    kms_project(project, kms_key)
    declared = {*EXPORTED, "NOENV", "EXONLY"}
    doc = json.loads(ok(project.run("env", "--json")).stdout)
    assert doc["set"] == {**EXPORTED, "EXONLY": "exec-only"}
    assert doc["missing"] == []
    assert set(doc["remove"]) & declared == {"NOENV"}
    shell = json.loads(ok(project.run("env", "--json", "--for", "shell")).stdout)
    assert shell["set"] == EXPORTED
    assert set(shell["remove"]) & declared == {"NOENV", "EXONLY"}
    keys = json.loads(ok(project.run("env", "--json", "--keys", "PLAIN,KTOK")).stdout)
    assert keys["set"] == {"PLAIN": "hello world", "KTOK": "kms value"}
    failed(project.run("env", "--json", "--keys", "NOT_DECLARED"))


def test_env_json_describe_lists_metadata_without_values(project, kms_key):
    kms_project(project, kms_key)
    doc = json.loads(ok(project.run("env", "--json", "--describe")).stdout)
    entries = {entry["key"]: entry for entry in doc["keys"]}
    assert set(entries) == {"PLAIN", "TRICKY", "MULTI", "EMPTY", "NOENV", "EXONLY", "DESCR", "KTOK"}
    assert entries["DESCR"]["description"] == "Described one"
    assert entries["NOENV"]["env"] is False and entries["PLAIN"]["env"] is True and entries["EXONLY"]["env"] == "exec"
    assert entries["EXONLY"]["injectable"] == {"exec": True, "shell": False}
    assert entries["NOENV"]["injectable"] == {"exec": False, "shell": False}
    assert "kms value" not in json.dumps(doc) and "hello world" not in json.dumps(doc)


def test_exec_injects_env_and_passes_status_and_arguments(project, kms_key):
    kms_project(project, kms_key)
    values, _ = child_env(project, names=["PLAIN", "KTOK", "MULTI", "EMPTY", "EXONLY", "NOENV", "TRICKY"],
                          env={"PLAIN": "inherited", "NOENV": "inherited-noenv"})
    assert values == {"PLAIN": "hello world", "KTOK": "kms value", "MULTI": "line1\nline2", "EMPTY": "",
                      "EXONLY": "exec-only", "TRICKY": TRICKY}
    status = project.run("exec", "--", "bash", "-c", 'printf "%s|" "$@"; exit 7', "argv0", "a", "b c", "--flag")
    assert status.returncode == 7
    assert status.stdout == "a|b c|--flag|"
    failed(project.run("exec", "--", "/nonexistent/command"))


def test_exec_child_reads_stdin(project, kms_key):
    kms_project(project, kms_key)
    result = ok(project.run("exec", "--", "bash", "-c", 'read -r line; echo "$line:$KTOK"', stdin="from-stdin\n"))
    assert result.stdout == "from-stdin:kms value\n"


def test_list_shows_declared_names_and_descriptions(project, kms_key):
    kms_project(project, kms_key)
    out = ok(project.run("list")).stdout
    for name in ("PLAIN", "TRICKY", "MULTI", "EMPTY", "NOENV", "EXONLY", "DESCR", "KTOK", "Described one"):
        assert name in out
    assert "kms value" not in out and "hello world" not in out
    values = ok(project.run("list", "--values")).stdout
    assert "kms value" in values and "hello world" in values
    sources = ok(project.run("list", "--sources", "--full")).stdout
    assert all(name in sources for name in ("KTOK", "PLAIN", "Described one"))


def test_config_discovery_walks_parents(project):
    project.write("fnox.toml", """\
[secrets]
PARENT_ONLY = { default = "p" }
SHARED = { default = "from-parent" }
""")
    child = project.mkdir("sub/deeper")
    project.write("fnox.toml", """\
[secrets]
CHILD_ONLY = { default = "c" }
SHARED = { default = "from-child" }
""", base=child)
    doc = json.loads(ok(project.run("export", "-f", "json", cwd=child)).stdout)
    assert doc["secrets"] == {"PARENT_ONLY": "p", "SHARED": "from-child", "CHILD_ONLY": "c"}
    # A config with root = true stops the upward search.
    project.write("fnox.toml", """\
root = true
[secrets]
CHILD_ONLY = { default = "c" }
SHARED = { default = "from-child" }
""", base=child)
    doc = json.loads(ok(project.run("export", "-f", "json", cwd=child)).stdout)
    assert doc["secrets"] == {"SHARED": "from-child", "CHILD_ONLY": "c"}
    failed(project.run("get", "PARENT_ONLY", cwd=child))


def test_local_and_hidden_config_files(project, kms_key):
    project.write("fnox.toml", f"""\
[providers.kms]
type = "aws-kms"
key_id = "{kms_key}"
region = "us-east-1"

[secrets]
PARENT = {{ default = "p" }}
SHARED = {{ default = "parent" }}
""")
    child = project.mkdir("child")
    project.write("fnox.toml", '[secrets]\nSHARED = { default = "child-main" }\nMAIN_ONLY = { default = "m" }\n', base=child)
    project.write("fnox.local.toml", '[secrets]\nSHARED = { default = "child-local" }\nLOCAL_ONLY = { default = "l" }\n', base=child)
    everything = {"PARENT": "p", "SHARED": "child-local", "MAIN_ONLY": "m", "LOCAL_ONLY": "l"}
    assert json.loads(ok(project.run("export", "-f", "json", cwd=child)).stdout)["secrets"] == everything
    assert json.loads(ok(project.run("-c", "fnox.toml", "export", "-f", "json", cwd=child)).stdout)["secrets"] == everything
    # An explicit path loads only that file: no parents, no local override.
    only = json.loads(ok(project.run("-c", "./fnox.toml", "export", "-f", "json", cwd=child)).stdout)["secrets"]
    assert only == {"SHARED": "child-main", "MAIN_ONLY": "m"}
    # Writes go to fnox.toml; the local file is left alone.
    local_before = project.read_bytes("fnox.local.toml", base=child)
    ok(project.run("set", "NEW", "v", "-p", "kms", cwd=child))
    main = project.config(base=child)["secrets"]
    assert set(main) == {"SHARED", "MAIN_ONLY", "NEW"}
    assert kms_plaintext(main["NEW"]["value"], kms_key) == b"v"
    main_before = project.read_bytes(base=child)
    failed(project.run("remove", "LOCAL_ONLY", cwd=child))
    assert project.read_bytes(base=child) == main_before
    ok(project.run("remove", "SHARED", cwd=child))
    assert set(project.config(base=child)["secrets"]) == {"MAIN_ONLY", "NEW"}
    assert project.read_bytes("fnox.local.toml", base=child) == local_before
    doc = json.loads(ok(project.run("export", "-f", "json", cwd=child)).stdout)
    assert doc["secrets"] == {**everything, "NEW": "v"}

    hidden = project.mkdir("hidden")
    project.write(".fnox.toml", '[secrets]\nHIDDEN = { default = "h" }\nSHARED = { default = "hidden" }\n', base=hidden)
    assert json.loads(ok(project.run("export", "-f", "json", cwd=hidden)).stdout)["secrets"] == {
        "PARENT": "p", "SHARED": "hidden", "HIDDEN": "h"}
    ok(project.run("set", "HNEW", "hv", "-p", "kms", cwd=hidden))
    assert set(project.config(".fnox.toml", base=hidden)["secrets"]) == {"HIDDEN", "SHARED", "HNEW"}
    # With both names present, .fnox.toml overrides fnox.toml, but writes go to fnox.toml.
    project.write("fnox.toml", '[secrets]\nBOTH = { default = "visible" }\nSHARED = { default = "visible-main" }\n', base=hidden)
    hidden_before = project.read_bytes(".fnox.toml", base=hidden)
    ok(project.run("set", "BNEW", "bv", "-p", "kms", cwd=hidden))
    assert project.read_bytes(".fnox.toml", base=hidden) == hidden_before
    assert set(project.config(base=hidden)["secrets"]) == {"BOTH", "SHARED", "BNEW"}
    project.write(".fnox.local.toml", '[secrets]\nSHARED = { default = "hidden-local" }\n', base=hidden)
    assert json.loads(ok(project.run("export", "-f", "json", cwd=hidden)).stdout)["secrets"] == {
        "PARENT": "p", "SHARED": "hidden-local", "BOTH": "visible", "BNEW": "bv", "HIDDEN": "h", "HNEW": "hv"}


def test_local_file_can_add_providers(project, kms_key):
    project.write("fnox.toml", 'root = true\n[secrets]\nA = { default = "a" }\n')
    project.write("fnox.local.toml", f'[providers.kms]\ntype = "aws-kms"\nkey_id = "{kms_key}"\nregion = "us-east-1"\n\n'
                  f'[secrets]\nLX = {{ provider = "kms", value = "{kms_ciphertext("local-secret", kms_key)}" }}\n')
    assert ok(project.run("get", "LX")).stdout == "local-secret\n"
    assert json.loads(ok(project.run("export", "-f", "json")).stdout)["secrets"] == {"A": "a", "LX": "local-secret"}


def test_explicit_config_and_global_config(project):
    project.write("fnox.toml", '[secrets]\nPROJECT = { default = "proj" }\nCOMMON = { default = "project-common" }\n')
    project.write("other.toml", '[secrets]\nOTHER = { default = "o" }\n')
    doc = json.loads(ok(project.run("-c", "./other.toml", "export", "-f", "json")).stdout)
    assert doc["secrets"] == {"OTHER": "o"}
    project.mkdir(".config/fnox", base=project.home)
    project.write(".config/fnox/config.toml", '[secrets]\nGLOBAL = { default = "g" }\nCOMMON = { default = "global-common" }\n',
                  base=project.home)
    doc = json.loads(ok(project.run("export", "-f", "json")).stdout)
    assert doc["secrets"] == {"GLOBAL": "g", "COMMON": "project-common", "PROJECT": "proj"}
    doc = json.loads(ok(project.run("--config", "./other.toml", "export", "-f", "json")).stdout)
    assert doc["secrets"] == {"GLOBAL": "g", "COMMON": "global-common", "OTHER": "o"}
    # FNOX_CONFIG_DIR relocates the global config.
    alt = project.mkdir("altcfg", base=project.root)
    project.write("config.toml", '[secrets]\nALT_GLOBAL = { default = "a" }\n', base=alt)
    doc = json.loads(ok(project.run("export", "-f", "json", env={"FNOX_CONFIG_DIR": str(alt)})).stdout)
    assert doc["secrets"] == {"ALT_GLOBAL": "a", "COMMON": "project-common", "PROJECT": "proj"}


def test_invalid_config_is_rejected(project):
    project.write("fnox.toml", '[providers.ps]\ntype = "aws-ps"\n\n[secrets]\nX = { default = "x" }\n')
    failed(project.run("get", "X"))  # region is required
    project.write("fnox.toml", '[secrets]\nX = { default = "x" \n')
    failed(project.run("get", "X"))


def test_exec_as_file_secret_and_redaction(project, kms_key):
    kms_project(project, kms_key, 'FILE_S = { default = "file-body\\nsecond", as_file = true }\n')
    script = 'printf "%s\\n" "$FILE_S"; cat "$FILE_S"; echo; echo "$KTOK $PLAIN"'
    result = ok(project.run("exec", "--", "bash", "-c", script))
    path, *rest = result.stdout.split("\n")
    assert path.startswith("/") and path != "file-body\nsecond"
    assert "\n".join(rest) == "file-body\nsecond\nkms value hello world\n"
    assert not os.path.lexists(path)  # removed once the command exits
    env = json.loads(ok(project.run("env", "--json", "--keys", "FILE_S,PLAIN")).stdout)
    assert env["files"] == {"FILE_S": "file-body\nsecond"} and env["set"] == {"PLAIN": "hello world"}
    redacted = ok(project.run("exec", "--redact", "--", "bash", "-c", 'echo "[$KTOK] [$PLAIN]"; echo "[$KTOK]" >&2'))
    assert redacted.stdout == "[[REDACTED]] [[REDACTED]]\n"
    assert "kms value" not in redacted.stderr and "[REDACTED]" in redacted.stderr
    plain = ok(project.run("exec", "--no-redact", "--", "bash", "-c", 'echo "[$KTOK]"'))
    assert plain.stdout == "[kms value]\n"


def test_exec_replace(project, kms_key):
    kms_project(project, kms_key)
    result = project.run("exec", "--replace", "--", "bash", "-c", 'echo "$KTOK"; exit 3')
    assert result.returncode == 3 and result.stdout == "kms value\n"
    failed(project.run("exec", "--replace", "--redact", "--", "true"))
    kms_project(project, kms_key, 'FILE_S = { default = "f", as_file = true }\n')
    marker = project.path("ran")
    failed(project.run("exec", "--replace", "--", "touch", str(marker)))
    assert not marker.exists()
