Routine configuration deployments have started causing database authentication failures on
our rotation-enabled installations. The application reads `AWSCURRENT` from Secrets Manager;
rotation runs independently of deployment. We also still need deployment to correct accidental
console edits. Disabling reconciliation isn't an acceptable workaround.

Repair the program in `/app` so deployment keeps the credential ownership policy in
`OPERATIONS.md`, including when a reviewed plan is applied later or an old version is no
longer available. Resource addresses are also reused when operators replace a backing
secret; delayed review-queue deliveries must respect replacement retirement. Preserve the CLI and durable record formats described in `README.md`.
Planning must remain read-only. Rotation's journal and downstream database belong to the
rotator, not the deployment program.

Run `python3 /app/bootstrap.py --out /tmp/site.json` for an ordinary local installation,
then `/app/recon plan --config /tmp/site.json --out /tmp/change.json` and
`/app/recon apply --plan /tmp/change.json`. The AWS endpoint is already running locally.
Processes may start in different working directories; keep durable coordination in AWS
services, not local files. Keep any pause announcements present in the starter if you
reimplement a command. Finish with your changes under `/app`.
