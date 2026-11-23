# syncctl

`syncctl` pushes a flat key/value source file into AWS Systems Manager Parameter Store or AWS
Secrets Manager. Each configured *sync* has a source file and one destination. The platform
CronJobs run `syncctl sync` for every sync on a schedule, and `syncctl remove` when a sync is
decommissioned.

```
bin/syncctl sync   --config CONFIG.json
bin/syncctl remove --config CONFIG.json
```

AWS access uses the standard SDK environment: `AWS_ENDPOINT_URL`, `AWS_DEFAULT_REGION`
(`us-east-1`), and `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. Nothing is hard-wired.

## Configuration

```json
{
  "sync_id": "payments-root",
  "destination": "ssm",
  "path": "/",
  "key_schema": null,
  "environment": "prod",
  "tags": {"team": "payments"},
  "disable_secret_deletion": false,
  "source_file": "payments.source.json"
}
```

| field | meaning |
|---|---|
| `sync_id` | Stable identifier of the sync. Two syncs never share one. |
| `destination` | `"ssm"` or `"secretsmanager"`. |
| `path` | SSM only. Parameter path that starts and ends with `/`. May be `/`. |
| `key_schema` | Optional template for destination names, using `{{secretKey}}` and `{{environment}}`, e.g. `INFISICAL_{{secretKey}}` or `{{environment}}/{{secretKey}}`. For SSM the path is prepended: path `/svc/` with schema `{{environment}}/{{secretKey}}` writes `/svc/prod/KEY`. |
| `environment` | Value for `{{environment}}`. |
| `mapping` | Secrets Manager only. `"one-to-one"` (default) writes one secret per key. `"many-to-one"` writes one secret, named by `bundle_name` (schema applied), whose value is the whole source as a JSON object. |
| `bundle_name` | Required for `many-to-one`. |
| `tags` | Resource tags applied to every entry the sync writes. |
| `disable_secret_deletion` | When true, `sync` never deletes anything. |
| `source_file` | JSON object of string values. Relative paths resolve against the config file's directory. Keys with an empty value are skipped. |

SSM values are written as `SecureString`.

## Ownership

Destinations are shared. The root path `/` in Parameter Store holds parameters from several syncs
and parameters ops maintains by hand. The Secrets Manager account holds everyone's secrets. What a
sync may touch is decided by ownership:

- A sync **owns** exactly the destination entries it created. Ownership is recorded on the entry
  itself, as the resource tag `syncctl:sync-id` with the sync id as its value. syncctl keeps no
  other record of what it created.
- A sync never modifies, retags or deletes an entry it does not own. That covers hand-managed
  entries and other syncs' entries, even when they sit under the same path, match the same key
  schema, or have exactly the name the sync wants to write.
- A desired name that is already held by an entry the sync does not own is a **conflict**. It is
  reported and left exactly as it is. The rest of the sync still runs.
- syncctl writes nothing into a destination except the synced entries themselves.

## Commands

`sync` makes the entries the sync owns match the source:

- it creates missing names;
- it updates owned entries whose value differs;
- it applies the configured `tags`;
- unless `disable_secret_deletion` is set, it deletes every owned entry that the current
  configuration no longer produces (a key removed from the source, or per-key secrets left
  behind after a switch to `many-to-one`).

Running `sync` again with an unchanged source changes nothing.

`remove` deletes every entry the sync owns, whether or not its key is still in the source.

Deletes are immediate and permanent. Secrets Manager deletes use no recovery window, so a removed
name can be synced again right away.

Output: one JSON line, `{"created": [...], "updated": [...], "deleted": [...], "conflicts": [...]}`
(destination names).

Exit status:

| code | meaning |
|---|---|
| 0 | done |
| 3 | done, but at least one desired name is a conflict |
| 2 | bad usage or configuration |
| 1 | failed |

See `docs/OPERATIONS.md` for how and where syncctl runs.
