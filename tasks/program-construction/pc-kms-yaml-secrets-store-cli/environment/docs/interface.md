# envelope-secrets-yaml — public interface

`envelope-secrets-yaml` is a command-line key/value store for infrastructure
secrets. Secrets live in a local YAML file; each stored value is protected by
envelope encryption: an AWS KMS data key encrypts the secret locally, and the
KMS-wrapped copy of that data key is stored next to the ciphertext. It can also
provision a KMS key for a *label* through AWS CloudFormation.

## Invocation

```
program [--help] [--version] <command> [<flags>] [<args>]
```

Run `program --help`, `program <command> --help` or `program help <command>`
for the authoritative flag list. Exit status is 0 on success and non-zero on
failure; diagnostics go to standard error. Flags may be written before or after
positional arguments; use `--` before a secret that begins with `-`.

## AWS endpoint and region

All AWS access goes through the AWS API. In this venue:

| Variable | Meaning |
|---|---|
| `AWS_ENDPOINT` | Base URL used for **every** AWS service call (e.g. `http://engines:4566`). The program does **not** read `AWS_ENDPOINT_URL`; set `AWS_ENDPOINT="$AWS_ENDPOINT_URL"`. |
| `AWS_REGION` | Region used when a key is referenced by a bare key id or `alias/...` name, and the default region preference for decryption. Use `us-east-1`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Credentials (dummy values are fine). |

When a key is referenced by a full ARN, the region in the ARN selects the KMS
region. The admitted scope uses the single region `us-east-1`.

## Secrets file

`-f/--filename FILE` (or `BISCUIT_FILENAME`) names the YAML file. It is a
mapping from a secret **name** to a **list of values**; each value records:

| Field | Meaning |
|---|---|
| `key_id` | The KMS key the value's data key was generated under |
| `key_manager` | `kms` |
| `algorithm` | `aesgcm256` or `secretbox` |
| `key_ciphertext` | base64 of the KMS-wrapped data key |
| `ciphertext` | base64 of the locally encrypted secret |

A secret may have several values (one per key it was encrypted under); any one
of them is sufficient to decrypt. The reserved name `_keys` holds the file's
*key template*: entries with `key_id`, `key_manager` and `algorithm` only,
which select the keys used by `put` when no `--key-id` is given. `_keys` is not
a secret.

Wrapped data keys are bound to the secret's name through the KMS encryption
context `{"SecretName": "<name>"}`; a value moved to a different name does not
decrypt. `aesgcm256` is AES-256 in GCM mode; `secretbox` is NaCl
`crypto_secretbox` (XSalsa20-Poly1305). Both use the 32-byte data key; how the
nonce is laid out within `ciphertext` is part of the observable format.
References: NIST SP 800-38D (GCM); D. J. Bernstein, "Cryptography in NaCl"
(secretbox).

## Commands in scope

### `put -f FILE [-k KEY[,KEY...]] [-a ALGO] [-i INFILE] <name> [<secret>]`

Encrypts `<secret>` (or the contents of `-i/--from-file INFILE`; giving both is
an error) and stores it under `<name>`, replacing any previous values for that
name. `-k/--key-id` takes a comma-separated list of KMS key references (full key
ARN, alias ARN, `alias/NAME`, or bare key id); one value is produced per key.
Without `-k`, the keys in the file's `_keys` template are used. `-a/--algorithm`
(or `BISCUIT_ALGORITHM`, default `secretbox`) chooses `aesgcm256` or
`secretbox` for keys given with `-k`. `-p/--key-manager` must be `kms` (the
default) in the admitted scope.

### `get -f FILE [-o OUTFILE] [-p REGIONS] <name>`

Decrypts one value of `<name>` and writes the plaintext to standard output
exactly (no newline is added when stdout is not a terminal), or to
`-o/--output OUTFILE`. `-p/--aws-region-priority` (default `AWS_REGION`) lists
regions whose keys are tried first; otherwise a name's values are tried in
the order they appear in the file.

### `list -f FILE`

Prints the secret names in the file, one per line.

### `export -f FILE [-p REGIONS]`

Decrypts every secret and prints the plaintexts as YAML (`name: plaintext`).

### `kms init -f FILE -r us-east-1 [-l LABEL] [-a ALGO] [flags]`

Ensures a KMS key exists for `LABEL` (`-l/--label`, `BISCUIT_LABEL`, default
`default`; 1–20 characters of `[A-Za-z0-9_-]`) in each region of
`-r/--regions` (`BISCUIT_REGIONS`; **the default is three regions, so always
pass `-r us-east-1` here**). A label's key is identified by the KMS alias
`alias/biscuit-LABEL`. If the alias is absent, the key is provisioned through a
CloudFormation stack named `biscuit-LABEL` and the alias is then created. The
file's `_keys` template is updated to refer to the key, with algorithm `-a`.
Other flags (`--administrators`, `--users`, `--create-simple-roles`,
`--disable-iam-policies`, `--create-missing-keys`) shape the provisioned
resources; `--cloudformation-template-url` is out of scope.

### `kms deprovision -r us-east-1 [-l LABEL] [--destructive]`

Reports the alias and stack belonging to `LABEL`. Only with `--destructive`
are the alias and the CloudFormation stack deleted.

## Out of scope

`kms grants ...`, `kms edit-key-policy`, `kms get-caller-identity`, the
`testing` key manager, the `none` algorithm, multiple regions, and
`--cloudformation-template-url`.

## Observation rules

* Results are judged by exit status (zero versus non-zero), by standard output
  of `get`, `list` and `export`, by files the program writes, and by AWS state
  observed through the AWS API. The state left behind by a *failing* command
  is compared as well.
* The secrets file is judged by its decoded content, not its byte layout. It
  is read with a YAML 1.1 safe loader (PyYAML `safe_load`); every field is a
  string. Value lists and `_keys` entries are compared as multisets: their order
  is not significant, but their number is.
* `get` output is compared byte for byte. `list` output is compared as a
  multiset of lines. `export` output is decoded as one YAML mapping with the
  same loader, and each plaintext must decode as the identical string.
* Freshly generated data keys and nonces make ciphertexts differ on every run.
  Stored values are checked by decrypting them independently: KMS `Decrypt`
  with the documented encryption context, then the stated cipher. Values
  written in the documented format by other tools must be readable as well.
  A value is decrypted through KMS in the region named by its `key_id`.
* For `kms init`, the stack's status, its resources and the parameters it was
  created with (names and values; the order of principals within a list
  parameter is not significant) are compared. So are the KMS key's description,
  the alias and its target, and any IAM roles the stack created. After
  `kms deprovision --destructive`, their absence is compared.
* Informational and diagnostic text (standard error, progress messages of the
  `kms` commands) is not compared. File permissions are not compared.
