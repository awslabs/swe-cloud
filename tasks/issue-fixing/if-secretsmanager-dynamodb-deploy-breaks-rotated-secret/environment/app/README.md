# Credential deployment service

This is a Python port of the secret-version resource's read/create/update lifecycle.
The current implementation follows the v5.33.0 lifecycle, while our rotation integration
has the operational contract in OPERATIONS.md. No infrastructure-as-code runtime is required.

`./recon plan --config SITE.json --out CHANGE.json` refreshes the deployment resource
and writes a reviewable JSON plan. It must not mutate any AWS resource.
`./recon apply --plan CHANGE.json` applies that plan in a new process.
`./recon apply --config SITE.json` combines the two operations.
A successful command returns zero. Each command has an operational allowance of 30 seconds;
rotation does not have to finish before deployment can succeed. Plans remain usable across
rotation activity. The scheduler does not run two deployment processes for one resource at
once, but it can run rotation between the plan and apply commands.

Configuration fields:

| Field | Meaning |
|---|---|
| resource_id | Deployment resource address, state partition key |
| secret_id | Existing Secrets Manager secret, name or ARN |
| secret_string | Initial credential used only when provisioning the first version |
| version_stages | Declared deployment label; currently `["AWSCURRENT"]` |
| state_table | DynamoDB deployment state table |
| ledger_table | DynamoDB rotation journal table |
| db_table | DynamoDB downstream credential table, used by rotation |

State has key `resource_id` (S), and fields `secret_id` (S), `version_id` (S),
`serial` (N). The version is the deployment's anchor, not a second secret copy. State writes
must be conditional on the serial read by the invocation (or on absence for initial creation).

Rotation journal has key `secret_id` (S). Fields: `state` is `finished` or `pending`,
`finished_version` (S) is the last acknowledged completion, `pending_token` (S) is the
current/last rotation token, `seq` (N) increments when a rotation starts. Deployment may read
this table consistently, but must not change it. The downstream table has key `user` (S),
with the application at `app`: `password_sha256` and `set_by_token` are written by rotation.
Deployment has no write ownership there either.

Plans include `config`, `anchor_before`, `observed_holder`, `target_version`, and `actions`.
An action is `create` or `move_current`; an empty list means no label work was planned.
Those fields should describe the refresh, even though apply may need a newer decision.
The starter also records `state_serial` and `prior_secret_id` for review. Additional
plan and state fields are allowed; existing fields retain their meanings. Local plan
files carry review data, not durable state.

`python3 rotation.py --config SITE.json` runs or resumes one rotation. `bootstrap.py` creates
isolated tables and a secret, deploys its initial credential, and initializes rotation records.
Use AWS CLI or boto3 for normal service inspection. All commands use AWS_ENDPOINT_URL
(default http://localhost:4566), region us-east-1 and local test credentials. Dependencies
are pinned in the image. No network service outside the container is required.

Venue note: Floci may attach a placeholder version when CreateSecret is called without a
value. This history does not replace the configured first deployment credential; initial deployment
of an address has neither deployment state nor a rotation journal. Replacement follows
the destination rules in OPERATIONS.md.

The resource address can be rebound by changing `secret_id` in a configuration, while
keeping `resource_id` and `state_table`. The destination secret already exists. Tables
can be shared across configurations; the journal is partitioned by secret identity.
`apply --config` is the scheduler's current desired binding. Saved plans come from its
review queue, which can deliver old work after a replacement. See OPERATIONS.md for
retirement of that work. Secret identifiers in this installation use canonical names;
ARN/name aliases and delete-and-recreate of a secret are outside this port.
