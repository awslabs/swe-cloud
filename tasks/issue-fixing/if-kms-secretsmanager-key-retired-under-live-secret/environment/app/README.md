# Sandbox sweep

The sweep empties our sandbox accounts on a schedule. It is a Python port of
[aws-nuke](https://github.com/ekristen/aws-nuke) and its run loop library libnuke, cut down to the
resource types our sandboxes use:

| type | what it is | removal |
|---|---|---|
| `S3EnvelopeObject` | any object in any bucket | back up, then `DeleteObject` |
| `SecretsManagerSecret` | any secret | back up, then `DeleteSecret` (no recovery window) |
| `KMSKey` | customer managed KMS keys | `ScheduleKeyDeletion` (7-day window) |

```
nuke.py                 CLI
libnuke/engine.py       scan, filters, the pass loop, give-up rule
libnuke/queue.py        items and their states
libnuke/registry.py     resource type registry (registration order = processing order)
libnuke/seam.py         named pause points (no-ops unless SEAM_SOCKET is set)
resources/              the three resource types, their listers, backup-before-delete
tools/make_sandbox.py   creates a demo sandbox and a config for it
```

## Running

```bash
python3 /app/nuke.py run --config sweep-config.json               # dry run: scan and print
python3 /app/nuke.py run --config sweep-config.json --no-dry-run  # remove
```

| exit | meaning |
|---|---|
| 0 | every item the sweep set out to remove is gone (a dry run always exits 0) |
| 2 | bad usage or config |
| 3 | the sweep stopped with items it could not remove (failed or held); the next sweep retries them |
| other | the sweep crashed |

The scheduler runs the sweep against each sandbox account with `AWS_ENDPOINT_URL` pointing at the
account gateway (below), one sweep at a time per account, and kills a sweep that is still running
after two minutes. A killed sweep reports nothing and its exit code is lost.

Locally, the emulator answers on `http://localhost:4566` (the image's default `AWS_ENDPOINT_URL`).
To run the way the scheduler does:

```bash
python3 /opt/sandbox/gateway.py --port 4567 &      # account gateway in front of the emulator
python3 /app/tools/make_sandbox.py --config-out /tmp/sweep.json
AWS_ENDPOINT_URL=http://localhost:4567 python3 /app/nuke.py run --config /tmp/sweep.json --no-dry-run
```

### Account gateway

`/opt/sandbox/gateway.py` forwards to the endpoint and applies the account's guard rails. Secrets
tagged `DeletionProtection=true` refuse deletion, the way protected RDS instances do, and only the
owning team can change that tag. `CancelKeyDeletion` leaves a key `Disabled`, as in AWS. The sweep's
role cannot enable keys.

## Config

```json
{
  "resource_types": ["S3EnvelopeObject", "SecretsManagerSecret", "KMSKey"],
  "filters": {
    "S3EnvelopeObject": [{"property": "Bucket", "value": "demo-keep"},
                         {"property": "Bucket", "value": "demo-backups"}]
  },
  "backup_bucket": "demo-backups",
  "settings": {"KMSKey": {"pending_window_days": 7}},
  "pass_sleep_s": 1,
  "feature_flags": {"wait_on_dependencies": false}
}
```

- `filters` keep resources: an item matching any filter of its type is `filtered` and never
  touched. Filter on any property the type exposes (`Bucket`, `Key`, `KmsKeyId`, `Name`,
  `tag:<key>`, `ID`, `State`, ...), with `type` `exact` (default), `glob`, `regex` or `contains`, and
  optionally `invert`.
- `backup_bucket` is where backup manifests go. Keep it filtered.
- `feature_flags.wait_on_dependencies` is libnuke's experimental type-level ordering. It is off in
  every sandbox config.

## How a sweep runs

1. **Scan.** Each type's lister runs once, in registration order (envelope objects, secrets, keys).
   Every resource found becomes a queue item. Its own `filter()` (e.g. keys already pending
   deletion, AWS managed keys) and the config filters may mark it `filtered`.
2. **Passes.** Each pass walks the queue in order: `new`, `hold` and `failed` items get `remove()`.
   It succeeds (`pending`), raises `HoldResource` (`hold`, tried again next pass) or fails (`failed`,
   tried again next pass). `pending`/`waiting` items are re-listed and become `finished` once gone.
   The pass then sleeps `pass_sleep_s`.
3. **End.** The sweep exits 0 when no item is left unfinished. It gives up (exit 3) after three
   consecutive passes in which only failed items were left.

Each pass begins by announcing the `pass_start` seam (`libnuke/seam.py`). Keep announcing it with
the same facts if you restructure the loop; the scheduler's tooling relies on it.

## Envelope convention

Application data in the sandboxes is envelope-encrypted client-side with KMS. The ciphertext
blob comes from `kms:Encrypt` under a customer managed key, with encryption context
`{"consumer": <consumer id>}`:

- **Objects**: the body is the ciphertext. User metadata `kms-key-id` names the key it was
  encrypted under, as the writer addressed it. Current writers record the key ARN; older ones the
  bare key ID. Consumer id: `s3://<bucket>/<key>`. Objects without `kms-key-id` are plain objects.
- **Secrets**: created with `KmsKeyId` set to the customer managed key (key ARN or key ID, whichever
  the owner passed). `SecretString` is the base64 of the ciphertext. Consumer id: the secret name.
  Secrets on the AWS managed key hold plain values.

Teams create and change these resources whenever they like, including while a sweep is running.

## Backup before delete

Before a secret or object is deleted, the sweep decrypts it and writes a manifest to
`backup_bucket` at `<type>/<urlencoded consumer id>.json`:

```json
{"consumer": "...", "type": "...", "key_arn": "...", "plaintext_sha256": "...", "size": 32, "taken_at": "..."}
```

A consumer that cannot be decrypted cannot be backed up and is not deleted.
