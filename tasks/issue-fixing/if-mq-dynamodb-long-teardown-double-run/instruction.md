# vm-destroyer: workers fall off vm.destroy and long VMs get destroyed twice

Platform-messaging moved us to their Amazon MQ broker (RabbitMQ 3.13) and declared `vm.destroy`
with a delivery acknowledgement timeout (`deploy/platform/vm.destroy.json`). They did it because
stuck consumers used to pin deliveries for hours, and the queue definition is theirs: it is not
changing for us.

Since the move:

- destroyer workers drop off `vm.destroy` one after another; on a busy day the queue backs up with
  no consumers at all until someone restarts the hosts;
- the cloud's teardown audit shows some of our biggest VMs (teardowns of several minutes are
  normal for them) with two teardowns running at the same time.

The code is in `/app` (`README.md` describes how it runs). Fix the destroyer so that:

- every requested VM ends up destroyed exactly once: one completed `vm-destroy` run per VM, never
  a second run of a VM that has already been destroyed, and never a second run of a VM whose
  current run is still printing progress, however long that run takes;
- a destroy is never lost when its worker dies part-way (host loss, OOM kill): another worker
  starts it again within 45 seconds;
- a `vm-destroy` run that hangs — the process stays alive and stops printing progress, while the
  worker and its broker connection are healthy — is given up and the VM started again within 45
  seconds of its last progress line; that was the deal with platform-messaging for keeping their
  timeout. Both 45-second limits are for a pool with a worker free to pick the VM up, which is
  how we size it: a worker runs one teardown at a time, and nobody expects a busy worker to take
  a second one. Which worker restarts the VM doesn't matter, as long as the dead or hung run no
  longer owns it;
- the monitoring expectations in the README hold;
- the work stays on the broker: don't move destroy requests to another queueing service, and
  leave `vm.destroy` as the platform declared it.

Workers are separate processes on separate hosts, each started from its own working directory;
nothing on local disk is shared between them. Keep `python3 -m destroyer.worker --worker-id <id>`
as the entry point with the environment the README lists, and keep running `vm-destroy` (from
`PATH`) as a child process of the worker, in its process group, so it dies with its host.

You can run everything locally: the README's *Local development* section brings up a broker with
the platform's queue definition, a fleet table, and a sandbox cloud API with teardowns of whatever
length you choose.
