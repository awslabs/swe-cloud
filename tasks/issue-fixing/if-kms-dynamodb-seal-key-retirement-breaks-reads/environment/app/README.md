# Seal service operations

This service stores application secrets as KMS envelopes in DynamoDB. It is a Python
port of the AWS envelope wrapper and same-type seal migration flow used by our old
seal service. It is intentionally a small service: local processes do computation,
AWS services hold durable state. IAM enforcement, cross-account use, KMS automatic
backing-material rotation and Vault core/unseal protocols are outside this service.

Run `bash /usr/local/bin/start_floci.sh`, then `python /app/initialize.py` for an empty
local installation. Python modules are in `/app`; use `PYTHONPATH=/app` when running
from another directory. boto3 and cryptography are installed. The endpoint is local
Floci 2.2.0, region us-east-1, credentials test/test. All clients must honor
`AWS_ENDPOINT_URL`. The network instrumentation may delay an individual AWS request
before delivering it, retaining the request bytes even if the sender disconnects;
a client's retries of a delayed request can be delayed the same way.
A response may also be lost after AWS has committed a request.

## Client protocol

`python -m seal writer --id west` runs until stdin closes. Each input line is JSON:
`{"req":"unique-request-id","op":"put","id":"secret-name","value_b64":"aGVsbG8="}`.
A successful response is `{"req":"unique-request-id","ok":true}`. Stdout is only
JSON replies; diagnostics go to stderr. An acknowledgement means the supplied value
has been durably committed, not queued for later repair. Requests to each writer
are serial. Several writers may run at once. Values are arbitrary bytes, including
empty values.

Clients keep a put's `req` when retrying after a missing response, and may reconnect
to another writer. A request ID is global to the installation, a nonempty string
of at most 128 UTF-8 bytes. Retries carry the same id and bytes, may overlap the
original attempt, and may arrive after newer puts, process restarts, or key
retirement. A successful put takes effect exactly once: its first commit advances
the entry's application revision by one; later retries acknowledge success without
writing its old value back or advancing that revision. Reusing a request ID with
different id or bytes must return `ok:false` and have no effect. Distinct requests
for one id are serial, but a retry of an older request can be in flight at any
time. There is no retry expiry or request-ID reuse within an installation. An
unacknowledged attempt may have committed; it must be resolvable by retrying.

`python -m seal get secret-name` prints the base64 value and exits 0. Missing entries
exit 2; operational errors are other nonzero exits. Reads during a put may return
the previous or the new value; after a new put is acknowledged they must return
its value until the next distinct put. A retry acknowledgement confirms the original commit and does not
change the current value. Migration is not an application put and must never undo
one. Restarting a process cannot change any acknowledged value.

## Key changes

The operator owns `seal_meta` item `pk=config`, with `key_id` (KMS key ID, ARN or
alias) and numeric `config_version`. An operator can replace the configured key and
increase config_version, or retarget an existing alias without changing this item.
Secrets from several former keys may coexist. Existing writers remain running.

After the change, run `python -m seal migrate`. Only one migrator is run at a time;
a crashed migrator is replaced by a fresh invocation. Key configuration is stable
for the duration of one migration. A completed command writes `seal_meta` item
`pk=migration` with `status=complete`, `target_arn` equal to the operator's new key's
ARN, and `config_version` equal to the config item. Operators wait for that record
and successful process exit before retirement. Completion authorizes retirement of
**every** former wrapping key, including keys used before the immediately previous
alias target. It must remain safe after delayed writer requests arrive. Existing
processes and future processes must keep using the completed target until the next
operator key change and migration. Migration may add coordination items to seal_meta;
it must not alter operator-owned config or aliases.

Keep the service online: healthy network puts and gets have a five-second response
budget, including while migration is in progress. A request delayed by the network
has that budget from delivery onward. Normal expected traffic is ten puts/second
across two active writers plus reads, with another writer potentially waiting for
network delivery. Migration has a 45-second budget over a few hundred envelopes,
including one process restart. Do not wait for disconnected writers to return before
completing. Keep former KMS keys Enabled; only the operator retires them. Existing
entries must remain readable both before and after key changes and throughout the
migration, including before migrate is invoked.

## Durable representation

Tables use PAY_PER_REQUEST. `seal_entries` has string partition key `id`; entries
contain `key_arn` (string), `wrapped_dek` (binary), `iv` (binary, 12 bytes), `ct`
(binary), and `version` (numeric application revision). Entries written by the
current release also carry `last_req` and `last_digest`: the request ID of the put
that last wrote the entry and the SHA-256 hex digest of its value. There are no sort keys.
`seal_meta` has string partition key `pk`. Pagination can occur on scans.

Each envelope uses a fresh random 32-byte AES key and AES-256-GCM, with no AAD.
`ct` is ciphertext followed by its 16-byte authentication tag. The DEK is wrapped
by KMS without encryption context; `key_arn` is the KeyId returned by that wrapping
operation. Preserve this representation: the backup/restore tooling reads it directly
with keyless KMS Decrypt and AESGCM, without consulting this service. Extra metadata
is allowed; alternate plaintext copies, deleting/expiring entries, and changing the
backup reader are not migration strategies. Application revisions increase on puts;
rewrapping preserves the value and its application revision.
