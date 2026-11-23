# Sandbox sweep retired the KMS key of a secret it failed to delete

Last night's sweep of one of our sandbox accounts couldn't delete the secret `payments/ledger-db`,
because it has deletion protection:

```
SecretsManagerSecret - payments/ledger-db - failed - ClientError: An error occurred (InvalidRequestException) when calling the DeleteSecret operation: Cannot delete protected secret, please disable deletion protection and try again.
```

In the same sweep it scheduled deletion of the customer managed key that secret is encrypted under.
Every sweep since has logged

```
KMSKey - 5e29d1cb-1b97-40e9-81ff-f77ba061a9b5 - filtered - is already in PendingDeletion state
```

and the secret can't be decrypted any more, so its owners can neither read it nor back it up. We
cancelled the key deletion by hand before the 7-day window ran out. That left the key Disabled, and
getting it re-enabled took a ticket to security. The sweep is our Python port of aws-nuke in `/app`;
start with `/app/README.md`.

Fix the sweep so this can't happen again. What we need from it:

- A key stays Enabled as long as anything encrypted under it still exists in the account. Sandboxes
  aren't frozen while a sweep runs, and some of what's in them is kept on purpose. When the sweep
  can't establish whether a key is still needed, it leaves the key alone. A key left behind is
  picked up by a later sweep; a key retired too early can't be put back. We can't lock teams out
  of a sandbox, so something first written under a key after the sweep last checked the place it
  lives in, in the moments before the retirement call, is a gap we accept. Anything that was
  already there when its place was checked is not.
- Keys whose data is all gone still get retired, in the same sweep where that happened. A key the
  sweep had to leave is reconsidered by the next sweep.
- Sweeps end on their own. The scheduler kills a sweep that is still running after two minutes,
  and a killed sweep reports nothing. Keep the exit codes the README documents.
- Everything else keeps working as it does now: backup before delete, the config format and
  filters, the CLI, deletion protection (never work around it), and the `pass_start` pause the run
  loop announces. The scheduler runs each sweep as a fresh process in a fresh working directory,
  with `AWS_ENDPOINT_URL` pointing at the account gateway. Nothing a sweep leaves on local disk is
  there for the next one.

The emulator is on `http://localhost:4566`. The README shows how to start the account gateway in
front of it, create a demo sandbox and run a sweep the way the scheduler does.
