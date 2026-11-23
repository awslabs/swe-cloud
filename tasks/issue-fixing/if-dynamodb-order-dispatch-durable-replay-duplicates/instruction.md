Order dispatch has become unreliable during worker recycling and delivery delays. Some accepted
orders never produce a receipt after a restart; others have dispatches from two executions even
though the original order was still waiting to finish. The normal submission path usually works. The incidents became more frequent after cooperative
worker replacement and gateway retry handling were enabled. Some callbacks now run twice in
one invocation, and an upstream write can succeed even though the worker sees an error.
Bundle dispatch has also exposed overlaps between different order payloads and reservations
left behind after an admission was refused. Large warehouse bundles can span hundreds of shared
delivery identities; the single-order smoke test does not exercise that path.

Repair the service in `/app` so an accepted execution can continue across invocations, competing
executions cannot dispatch the same order while it can still resume, and abandoned orders become
available again when the runtime stops allowing their execution to resume. Preserve completed
response caching and the operational storage contract. See `/app/README.md` for the local runtime
and commands.

Workers run in fresh processes and separate working directories. Durable coordination must live
in the emulated AWS services, not local files or process memory. Keep the decorator/context API
and the pause announcements the starter emits; the runtime uses those integration points.
