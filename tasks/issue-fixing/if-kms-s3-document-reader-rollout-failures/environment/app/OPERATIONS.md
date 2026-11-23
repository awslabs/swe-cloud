# Document service

The fleet stores one JSON document per S3 object. KMS holds the wrapping keys;
there is no database copy of the plaintext. We are moving from the old keyservice
client to callers that use each entry's configured encryption context directly.

Run `python /app/seed.py --bucket sopsdocs` once for an ordinary local deployment.
Then run `python /app/migrator.py --bucket sopsdocs --prefix docs/ --barrier-key
control/legacy-fleet-retired` in each reconciler process, and read with
`python /app/newsops.py decrypt --bucket sopsdocs --key docs/accounts.json`.
The reader prints the UTF-8 payload plus a newline, exits zero, and never writes
anything. A read must complete within 10 seconds, including before reconciliation.
`python /app/legacy.py --help` describes the still-deployed client. Editing that
local copy does not change the deployed old fleet; its behavior is fixed for this rollout.

Deployments normally run two reconcilers. They may overlap, die without cleanup,
and restart with identical arguments. They must stop within five seconds of SIGTERM.
Each process uses AWS_ENDPOINT_URL, us-east-1 and the local test credentials. The
supervisor may interpose an AWS endpoint and use the existing pre-publication pause
protocol. Preserve those announcements in any replacement write path.

During the rollout, any old-client read that worked on the current document must
continue working, for every old request ordering that worked, until retirement.
An old edit or rotate establishes the new current document; acknowledged edits
must not disappear. Old jobs do not cooperate with new-client locks. They always
use the original object key, without S3 versions. Their version stamp is a client
release label, not an inventory of which processes have touched the document.

Within 15 seconds of startup, and again within 15 seconds of any old write, every
configured (group, KMS ARN) in every document must have a wrapping that opens with
the configured context directly and participates in a payload-authenticated quorum.
Direct-context consumers must tolerate the loss of any groups beyond the threshold:
every threshold-sized group subset, with any configured ARN chosen in each group,
must contain an authenticating quorum. They can try alternative entries within
those chosen groups and ARNs. Recovery in the new reader is necessary but
is not a substitute for this stored coverage: other consumers use plain KMS Decrypt.

Only the existence of the supplied barrier key means the old fleet has retired.
It is absent until all old jobs have stopped and is never removed. Within 20 seconds
of its appearance, every remaining entry must open with its configured context
and participate in a quorum authenticating the current payload; obsolete and damaged wrappings
must be gone. Do not use wall-clock estimates of retirement.

Every published document must retain the original group coordinates, threshold,
ARN set within each group, and configured contexts. Each (group, ARN) must retain
a wrapping that can participate in a quorum authenticating the current payload.
Migration must preserve payload ciphertext bytes; only old jobs may reseal them.
Keep the docs/ object-key set and payloads, except for changes made by old jobs. A partial repair must never be published.
Use S3 PutObject on existing keys for document publication. Updates contend
with unconditional old puts, not just with each other.

Transport note for this emulator: a rejected conditional S3 PutObject (412) can
poison its HTTP connection. Discard that S3 client before retrying; 409 also means
refresh and retry. If-None-Match and conditional CopyObject are not supported here.
KMS encryption context is authenticated data, not a label that can be changed in
JSON to rebind a ciphertext. The endpoint models the key permission tiers described below.

Recovery inventory: the deployment contains threshold-key-group documents as well
as older single-group documents. Disaster-recovery imports retain master-key
entries from different rotations and share refreshes; their array order and
creation timestamps are not authority for the current sharing. Old rotate creates a fresh key and a fresh sharing, overwrites every entry's wrap for its group, and retains the threshold
and group configuration. Old edit retains wrapping bytes and group layout.
Different groups and ARNs can have different configured contexts, including empty
and single-value maps. These are supported throughout the rollout, not just on
single-group documents. Neither writer retains unknown migration metadata. 

Imported archive keys can be in the rollout principal's Decrypt-only tier. The
stable deployment inventory at `control/keys.json` lists these ARNs in
`decrypt_only`; a missing list means all listed keys allow encryption. Existing
ciphertexts still decrypt under their actual bindings, but Encrypt,
GenerateDataKey and ReEncrypt to those destinations return AccessDeniedException.
Other listed keys permit Encrypt and Decrypt. These permissions do not change
during the rollout. The local permission adapter and the supervisor's AWS gateway
apply this policy because the emulator does not implement production IAM policy.
Do not change the inventory, key policies, or permission tier as part of this fix.

Archives with restricted keys have retained direct-context recovery material.
They may have several current share refreshes. The stored inventory admits the
same threshold failover coverage required above using existing restricted-key
ciphertexts and new wrappings on writable keys. Old jobs can edit these archives;
rotation jobs target only documents whose keys all permit encryption. Existing
archive ciphertexts cannot be recreated by the rollout principal. Their configured
ARNs and threshold remain part of the recovery policy even after retirement.
