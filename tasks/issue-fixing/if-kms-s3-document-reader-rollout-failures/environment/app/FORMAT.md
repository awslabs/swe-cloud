# Object format

Objects are UTF-8 JSON. `sops_version` is a string, `lastmodified` an ISO timestamp.
`payload` has base64 `nonce` (12 bytes) and `ciphertext` (AES-256-GCM including tag).
Its key is the 32-byte data key, and its AAD is the UTF-8 S3 object key.
`kms` is a nonempty ordered list of entries:

    {"arn": "arn:aws:kms:...:key/...", "context": {"app": "accounts", "env": "prod"},
     "enc": "<base64 KMS CiphertextBlob>", "created_at": "<ISO timestamp>"}

Without `shamir_threshold`, entries wrap the 32-byte payload data key and belong
to group 1. Threshold documents have `shamir_threshold` >= 2 and every entry has
an integer `group` in 1..255. Groups are Shamir coordinates, not array offsets.
KMS plaintext is then a 33-byte share: coordinate byte followed by 32 ordinates.
Any threshold number of distinct groups on the same polynomial reconstruct the
payload key. `shares.py` implements the arithmetic and wire representation.
Group counts, coordinates and thresholds vary between documents. The order of groups is
their order of first appearance in `kms`; entry order within each group is its
relative list order. The flat JSON representation encodes ordered key groups:
readers visit groups in that order and finish a group's alternatives before
moving to the next group, even when entries are interleaved in the JSON list.

An ARN may appear in several groups, wrapping different parts. Inside a group an
ARN may appear more than once, always with the same configured context. Reads
succeed only when a reconstructed key authenticates the AES-GCM payload. KMS
success authenticates the blob and its context, not its membership in the current
payload's sharing. Retained entries can come from historical rotations or share
refreshes; several entries in a group need not wrap the same ordinate. A share
refresh can change the polynomial while keeping the payload key. Some retained
blobs can be damaged. Available entries always contain at least one quorum that
authenticates the current payload. See legacy.py for the deployed reader's
first-decryptable-entry-per-group behavior. It combines every successfully
opened group (requiring at least the threshold), without retrying other selections
if payload authentication fails; it predates retained recovery entries.
An empty context is represented by `{}`. The fields describe configuration;
the actual KMS binding is inside `enc`. Unknown fields are permitted by new
clients; old writers retain only the fields listed above.

Old request ordering is represented by integer `--pick`: choose a key from the
sorted context keys (modulo the key count). The old keyservice uses that ordering
when converting its request context. Picks are deterministic here to make old
operations reproducible. Rotation can use a separate `--write-pick` for its
Encrypt calls: read and write KMS invocations do not share request ordering. Inspect legacy.py for old request and writer behavior.
`control/keys.json` lists the deployment's KMS ARNs for operators as
`{"arns": [...], "decrypt_only": [...]}`. The optional `decrypt_only` subset
is the stable permission tier of the rollout principal, not migration progress.
See OPERATIONS.md for its operational meaning.
