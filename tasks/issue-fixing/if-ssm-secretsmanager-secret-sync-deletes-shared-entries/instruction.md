**PLAT-2291 — syncctl is destroying and orphaning entries on shared destinations**

We moved the shared-root secret syncs onto `syncctl` (`/app`) last sprint. Since then:

- **Parameter Store, path `/`:** the second `payments-root` sync after onboarding deleted every parameter it had just pushed. The run after that recreated them. Consumers saw the secrets flap in and out.
- When `checkout-root` synced, ops' hand-managed parameters at `/` disappeared. Ops restored them from their records.
- `syncctl remove` for a decommissioned sync at `/` reported success and left its parameters behind.
- **Secrets Manager:** switching the `reports` sync from one secret per key to a single bundle secret force-deleted unrelated secrets belonging to other teams. There is no recovery window, so they were gone for good.

Fix `syncctl` so the guarantees in `/app/README.md` ("Ownership" and "Commands") actually hold, under the conditions these jobs really run in (`/app/docs/OPERATIONS.md`). Keep the CLI (`/app/bin/syncctl sync|remove --config CONFIG.json`), the config format, the output line and the exit codes as documented. syncctl must keep working for both destinations, both Secrets Manager mappings, key schemas and configured tags.

Each invocation of `/app/bin/syncctl` is its own process, in a fresh working directory with fresh `HOME` and `TMPDIR`, and with the AWS endpoint, region and credentials taken from the standard SDK environment variables. Nothing it leaves on local disk is available to the next run.

The emulator is at `http://localhost:4566` (`bash /usr/local/bin/start_floci.sh` starts it if it is down). The `aws` CLI is installed.
