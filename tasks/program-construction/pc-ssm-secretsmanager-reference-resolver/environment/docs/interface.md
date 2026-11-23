# ref-expander: public interface

ref-expander is a command-line program that reads a YAML/JSON document (or a single
string) and replaces the *reference expressions* in it with values fetched from AWS:
SSM Parameter Store, Secrets Manager or KMS decryption. Everything that is not a
reference passes through.

The reference executable is `/reference/program`; your build must produce `/app/program`
with the same observable behaviour. It reads no configuration files and keeps no state
between runs; all AWS access goes through the AWS API.

## AWS endpoint and credentials

| Variable | Meaning |
|---|---|
| `AWS_ENDPOINT_URL` | Endpoint for every AWS client (here `http://engines:4566`) |
| `AWS_REGION` | Default region (`us-east-1`) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Credentials (dummy values here) |

A per-reference `region=` parameter overrides the region for that reference.

## Commands

```
program eval [-f FILE|DIR|-] [-o yaml|json] [-s] [-exclude-secret] [-fail-on-missing-key-in-map=BOOL]
program get [-s] 'EXPRESSION'
program env [-export] -f FILE|-
program exec [-f FILE] [-i] [-s] [--] COMMAND [ARGS...]
```

Flags use Go-style syntax (`-flag`, `-flag=value`, `-f value`) and come before
positional arguments.

- `eval` reads YAML/JSON (`-f -`, the default, reads standard input; a directory
  reads every file in it), resolves references in string values, and writes the
  result as YAML (default) or JSON (`-o json`). The input may hold several
  `---`-separated documents. Input is parsed with YAML 1.2 rules: `yes`, `on` and
  `off` are strings, and dates are kept as strings.
  `-exclude-secret` leaves `secretref+` expressions unresolved.
  `-fail-on-missing-key-in-map` (default `true`) decides whether a fragment naming
  an absent key is an error.
- `get` expands the expressions in one string argument and prints the result with
  no trailing newline.
- `env` resolves a flat map of variable names to values and prints `NAME=value`
  lines for a POSIX shell, quoting values when needed. `-export` prefixes each
  line with `export `.
- `exec` resolves the same kind of map, runs `COMMAND` with those variables as its
  environment, and passes standard input, output and error through. `-i` also
  inherits the caller's environment.
- `-s` (silent) suppresses informational log lines on standard error.

On failure the program prints a diagnostic on standard error and exits non-zero.

## Reference expressions

```
ref+BACKEND://PATH[?PARAMS][#FRAGMENT][+]
secretref+BACKEND://PATH[?PARAMS][#FRAGMENT][+]
```

- `BACKEND` is `awsssm`, `awssecrets` or `awskms`.
- `PARAMS` are `key=value` pairs joined with `&` (URL query syntax and escaping).
- `FRAGMENT` is a `/`-separated key path into the fetched value parsed as YAML/JSON,
  e.g. `#/db/password`. The value found keeps its type when the expression is the
  whole string value.
- An expression runs until whitespace, a newline, a `+` or the end of the string,
  and does not end with a `"` or `,`. A trailing `+` ends an expression explicitly
  so it can be embedded, e.g. `url: "https://ref+awsssm://app/host+:8443/"`.
- A reference's path may itself contain a reference, which is resolved first.
- References without intervening whitespace are scanned as nested, even across
  terminating `+` characters and literal text. Later references are resolved first
  and must yield scalars. For example, if `app/doc` contains
  `{"port":5432,"l":[1,"two"]}`, then
  `p=ref+awsssm://app/doc?mode=singleparam#/port+;l=ref+awsssm://app/doc?mode=singleparam#/l+`
  fails because the later reference yields a list. Reversing the two references
  produces `l=[1 two];p=5432`; inserting whitespace between them allows both
  values to be embedded independently.
- `secretref+` is resolved like `ref+` unless `-exclude-secret` is given.

### `awsssm`: SSM Parameter Store

```
ref+awsssm://PATH/TO/PARAM[?region=R&version=N]
ref+awsssm://PREFIX[?region=R&recursive=true]#/PATH/UNDER/PREFIX
ref+awsssm://PARAM?mode=singleparam[&version=N]#/KEY/IN/VALUE
```

A leading `/` is implied for the parameter name. SecureString values are decrypted.
Without a fragment a single parameter is read; `version=N` selects a historical
version. With a fragment and no `mode`, the path is a *prefix* whose parameters are
arranged into a nested map by name; `recursive=true` includes deeper levels. With
`mode=singleparam` the parameter's value is parsed as YAML/JSON and the fragment
selects within it.

### `awssecrets`: Secrets Manager

```
ref+awssecrets://SECRET_NAME_OR_ARN[?region=R&version_stage=STAGE&version_id=ID][#/KEY/PATH]
```

Without a fragment the reference yields the secret value (string or binary). With a
fragment the value is parsed as YAML/JSON. `version_stage` (`AWSCURRENT`,
`AWSPREVIOUS` or a custom label) and `version_id` select a version.

### `awskms`: KMS decryption

```
ref+awskms://URLSAFE_BASE64_CIPHERTEXT[?context=URLENCODED_YAML_OR_JSON&alg=ALG&key=KEY][#/KEY/PATH]
```

The ciphertext blob is base64 with the URL-safe alphabet (`-` and `_` instead of `+`
and `/`), padding included. `context` is a URL-encoded YAML or JSON mapping of
string keys to string values, e.g. `context=%7B%22app%22%3A%22web%22%7D` for
`{"app":"web"}`. YAML flow mappings such as `{app: web, env: prod}` are also
accepted when URL-encoded. `key` is an optional
(URL-encoded) key id, ARN or alias; `alg` names the encryption algorithm
(`SYMMETRIC_DEFAULT` for symmetric keys). With a fragment the plaintext is parsed
as YAML/JSON.

## Admitted scope

Graded: the four commands and flags above, the three backends with the parameters
listed, symmetric KMS keys, and documents whose mapping keys are plain strings.

Not graded: other backends or commands (`flatten`, `ksdecode`, `version`, help
text), `-decode-kubernetes-secrets`, `-stream-yaml`, `profile=`/`role_arn=`,
asymmetric KMS keys, references inside mapping keys, percent-escaped fragments,
YAML timestamps inside fetched values, recursive prefixes holding both `/x` and
`/x/...` parameters, the meta-listing fallback for absent secrets, how often AWS is
called, and standard error wording.

## Observation rules

- `eval` output is decoded and compared as data, document by document. In JSON
  output each document is one JSON value and documents are separated by a line
  containing only `---`; YAML output is a normal YAML stream decoded with PyYAML
  `yaml.safe_load_all`. Mapping key order and whitespace do not matter. Strings,
  booleans, null, numbers, mappings and sequences are distinct (the string `"5432"`
  is not the number `5432`, `true` is not `1`); numbers compare by value (`2`,
  `2.0` and `2e0` are equal). Sequence order matters. When `-f` names a directory,
  the order of its files is not graded.
- `get` output and `exec` child output are compared byte for byte.
- `env` output and the environment seen by an `exec` child are compared as lines,
  each exactly, in any order.
- Exit status is compared as zero vs non-zero. A failed `eval`, `get` or `env` writes
  nothing to standard output. With `-s`, a successful `eval`/`get`/`exec` writes
  nothing of its own to standard error.

Establish the rest by running `/reference/program`: how prefixes become maps and
which path/prefix/fragment combinations fail, how `get` and `eval` differ on missing
keys, how non-string values are rendered when embedded in a larger string, how
sequences at document top level are treated, how version/stage/context parameters
combine, how `env` quotes, and how `exec` builds the child environment.

## Example

```sh
python3 -c 'import boto3; boto3.client("ssm").put_parameter(Name="/demo/db/host", Value="db.internal", Type="String")'
cat > doc.yaml <<'EOF'
db:
  host: ref+awsssm://demo/db/host
  url: "postgres://ref+awsssm://demo/db/host+:5432/app"
replicas: 3
EOF
/reference/program eval -s -f doc.yaml -o json
```
