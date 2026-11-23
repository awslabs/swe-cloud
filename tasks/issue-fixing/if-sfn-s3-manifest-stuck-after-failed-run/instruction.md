# Manifest downloads stay broken after a failed generation

The catalog index has been timing out under load since last week, and the portal team keeps
forwarding the same complaint: some manifests can never be downloaded again. Each time, one
generation of that manifest failed (the generator reported `IndexUnavailable`). After that, every
request for the same manifest ends like this, whether it comes from a new browser session, a fresh
`PUT` from a script, or a request made hours later:

```
GET /v1/manifests/<token>
HTTP 500 {"Code": "ServerError", "Message": "('Manifest generation failed', 'FAILED', 'IndexUnavailable')"}
```

The generator is never asked to try again. So far only a reindex of the catalog, which changes every
manifest key, has made these manifests downloadable. The index team expects timeouts to continue for
a while. On bad days the same manifest has failed dozens of times in a row before the index
recovered. Once the index is healthy, the next request for a manifest has to produce it.

Please fix the manifest service (`/app/manifest_service`) so that failed generations stop stranding
manifests. These have to keep working:

- **Shared generation.** Generation is expensive. Requests for the same manifest that arrive
  together have to share one generation. That covers the portal fanning out, scripts retrying in a
  loop, and requests landing on different service instances. It also covers the rush of retries
  right after a failure.
- **Generation only on request.** The service starts a generation only on behalf of a client
  request.
- **Failed polls stay failed.** A client polling the token of a failed attempt is still told that
  it failed. The portal relies on that 500 to show the error and offer "try again", which sends a
  fresh `PUT`.
- **Cache and expiry.** A manifest that is in the bucket is served from the bucket. A manifest that
  has expired from the bucket is generated again when someone asks for it.
- **Fixed interfaces.** The HTTP API, the execution input and output agreed with the generator, and
  the configuration variables stay as they are (see `/app/README.md`). The state machine and the
  generator belong to the data team and are not part of this change.

Service instances are separate processes with no shared memory or disk, started as
`python3 -m manifest_service --port <port>` with the environment described in the README. They are
restarted on every deploy and whenever they crash, so anything they need to agree on must live in AWS.
A deployment gives the service the state machine and the bucket named in its configuration and
nothing else.

`/app/README.md` describes the architecture, the API and how to run the service, the generator and
the emulator locally.
