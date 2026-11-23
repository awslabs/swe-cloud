# chambersm loses secret history, and some acknowledged writes disappear

`/app` is chambersm, our Python port of chamber's Secrets Manager backend. Deploy pipelines set
service secrets with it (`ci/set-secret.sh`). On-call uses `chambersm history` and
`chambersm read --version` to see what a key was set to, and by whom, and to roll back.

Three problems have been reported.

**History drops versions.** On a fresh service:

```
cd /app
python3 -m chambersm write demo key1 value1
python3 -m chambersm write demo key1 value2
python3 -m chambersm write demo key2 value1
python3 -m chambersm history demo key1      # version 1 is gone
```

Other sequences of writes lose other versions.

**Acknowledged writes vanish.** Twice in the last month a `set-secret` job exited 0, but its
value was not the service's current value afterwards and did not show up anywhere in history.
Both times another pipeline was setting the same key of the same service at about the same
time. We have also had `set-secret` jobs killed by the step timeout in the middle of a write,
and nobody is sure what state those left behind.

**Writes to `payments` fail.** Since this week every `set-secret` job that sets
`payments/db_password` fails like this:

```
chambersm: An error occurred (LimitExceededException) when calling the PutSecretValue operation:
You can't attach more than 20 staging labels across all versions of a secret. This request would
attach 21.
```

`db_password` there has been rotated daily for a few weeks; `webhook_secret` was added a few days
ago. Other busy services will get there too.

Please fix chambersm so that, when it runs the way we run it (see "Operating notes" in
`/app/README.md`):

- a write that exits 0 is never lost. Its value stays current until a later write replaces it,
  and `history` and `read --version` keep showing it for as long as "History retention" in the
  README says;
- history never contradicts itself or the current value. Each version of a key has one value,
  and a write that finished before another one started has the lower version number;
- a write that did not exit 0 (killed, timed out) may or may not have taken effect. Either way
  it must not leave history inconsistent, and it must not stop later writes from succeeding;
- concurrent writes to the same service each succeed, and no write waits on another job;
- this holds for the services we already have, `payments` included, as they are now. We cannot
  recreate their secrets.

Keep the command line, the output formats and the storage format as they are, because other
tools read the secret directly. Writers have only the permissions in `iam/chambersm-writer.json`.
A Floci emulator is running at `http://localhost:4566` for you to work against.
