# stackctl — the local stack engine

In this environment there is no CloudFormation and no Lambda; `stackctl` stands in for both. It
implements the part of CloudFormation's stack behaviour and custom-resource protocol our stacks use,
with the same semantics. Where this page and the AWS documentation for custom resources agree, the
AWS documentation is the longer explanation; where they differ, this page is what the engine does.

## Stacks, templates, revisions

An app revision is a directory with a `manifest.json` and one template per stack:

```json
{"stacks": [{"name": "SharedNetwork", "template": "SharedNetwork.template.json"},
            {"name": "Storefront", "template": "Storefront.template.json", "dependsOn": ["SharedNetwork"]}]}
```

`stackctl deploy <revision>` deploys the stacks in manifest order (dependencies first, which is how
CDK orders a producer and the consumers that reference it) and stops at the first stack that does
not reach `CREATE_COMPLETE` / `UPDATE_COMPLETE`. `--exclusively <stack>` deploys only the named
stacks.

A template is `{"Resources": {LogicalId: {"Type": ..., "Properties": {...}}}}`. Resources are
processed one at a time in template order.

Resource types:

| Type | Behaviour |
|---|---|
| `Local::Function` | Stands in for `AWS::Lambda::Function`: `Properties.Handler` (a Python module) and `Properties.Code` (a code asset). See *Functions*. |
| `Custom::<Name>` | Custom resource. `Properties.ServiceToken` is the ARN of the function that handles its requests, written `{"Fn::GetAtt": [<function logical id>, "Arn"]}` (see *Handlers*). |
| `Local::Hook` | Runs `Properties.Command` (argv) on Create and Update; a non-zero exit fails the resource. Delete is a no-op. Used for smoke tests and post-deploy checks. |
| `Local::Service` | A service configured with `Properties.Environment` (name → value). On Create and Update its `HealthCheck` (argv) runs with those variables set; a non-zero exit fails the resource. Delete is a no-op. |

### Functions

A custom resource's handler is part of the stack, as a CDK custom-resource provider is: the template
has a `Local::Function` whose `Code` is `{"Asset": "<directory>"}`, a directory of the app
(`crossref` here). Like `cdk deploy`, `stackctl deploy` publishes a stack's assets before it touches
the stack: it copies the directory into the asset store (`$STACKCTL_HOME/assets/<hash>/`) and the
template it deploys says `{"AssetHash": "<hash>"}` instead. Editing the directory therefore changes
the function's properties, and the next deploy of the stack updates the function in place to the new
code; until then the stack keeps running the code it was deployed with. A function is a resource like
any other: it is updated in template order, and rolled back like any resource updated in place.

The function's physical id is its ARN, `arn:aws:lambda:<region>:000000000000:function:<stack>-<logical id>`,
which is what `Fn::GetAtt [<logical id>, "Arn"]` resolves to (the only `Fn::GetAtt` the engine knows).
A request to a custom resource is handled by the code its function has at the moment the request is
sent.

### Dynamic references

A property string may contain `{{resolve:ssm:<parameter name>}}`. The engine resolves it with
`GetParameter` each time it applies the resource's properties: on Create, on Update, and again on
the Update that rolls a resource back to its previous properties. If the parameter does not exist
the operation fails with `Parameters: [ssm:<name>] cannot be found.` and the handler is not called.
Resolved values are what the handler receives.

## Updates

A resource is only touched when its template properties (or type) change; an unchanged resource
gets no request at all, even if a parameter it references has changed. A changed resource gets an
`Update` request carrying `ResourceProperties` (the new, resolved properties) and
`OldResourceProperties` (the properties it last applied successfully), and its current
`PhysicalResourceId`.

The `PhysicalResourceId` in the Update response decides what kind of update it was:

* **Same id** — an in-place update.
* **Different id** — a *replacement*: the engine records the new physical resource and carries on
  with the rest of the stack. The old physical resource is not touched yet. Once every resource
  has been applied the stack update has succeeded: the stack enters
  `UPDATE_COMPLETE_CLEANUP_IN_PROGRESS` (from here on the new state is the stack's committed
  state) and the engine sends `Delete` for the old physical id, with the old properties. Resources
  removed from the template are deleted in the same phase. Then `UPDATE_COMPLETE`.

A Delete that fails during a cleanup phase is retried; after 3 failed attempts it is abandoned
(logged as `abandoned`) and the stack still completes.

## Failure and rollback

A `FAILED` response (or a dynamic reference that cannot be resolved) fails the update. The stack
enters `UPDATE_ROLLBACK_IN_PROGRESS` and the resources this update already touched are rolled back
in reverse order:

* a resource updated in place — including the one whose Update just failed — gets another `Update`
  with the properties swapped: `ResourceProperties` are its previous properties (re-resolved now)
  and `OldResourceProperties` are the ones it was being updated to. It keeps its previous physical
  id, whatever that Update answers. A function rolled back this way runs its previous code from
  then on;
* a resource that was replaced, or created, in this update is left alone for now; once the reverse
  pass is over, in `UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS`, the engine sends `Delete` for the
  *new* physical id with the new properties (retried and abandoned as above). The previous physical
  resource is kept as it was.

Then `UPDATE_ROLLBACK_COMPLETE`, and the stack is back to its previous committed state.

If any request during the rollback fails, the stack ends in `UPDATE_ROLLBACK_FAILED`. The engine
will not update such a stack again; in AWS this needs someone to repair whatever blocked the
rollback and run `continue-update-rollback`. Locally, `stackctl reset` starts over.

A failure while *creating* a stack deletes what was created (`ROLLBACK_COMPLETE`); such a stack must
be destroyed before it can be deployed again.

## Handlers

Each request runs one handler process: `python3 -m <Handler>` of the function named by the
ServiceToken, with `PYTHONPATH` set to that function's code asset (so a handler sees only its asset,
not `/app`). The request is a CloudFormation custom-resource request as JSON on stdin:

```json
{"RequestType": "Create|Update|Delete", "RequestId": "<uuid>", "StackId": "arn:aws:cloudformation:...:stack/<name>/<uuid>",
 "LogicalResourceId": "...", "ResourceType": "Custom::...", "PhysicalResourceId": "... (Update, Delete)",
 "ResourceProperties": {...}, "OldResourceProperties": {... (Update only)}}
```

The handler writes one JSON line to stdout:
`{"Status": "SUCCESS|FAILED", "PhysicalResourceId": "...", "Reason": "...", "Data": {...}}`.
A Delete must answer with the physical id it was given. `crossref/entrypoint.py` implements this
for our handlers.

The handler runtime, like Lambda's, keeps nothing between requests:

* one request at a time; the engine waits for the answer before it sends the next request;
* each process starts in a fresh, empty working directory that is also its `HOME` and `TMPDIR`;
  when the engine runs as root the handler runs as the unprivileged user `cfnhandler`;
* as soon as the response line has been read (or the process exits, or 30 s pass without an
  answer, which counts as `FAILED`) the handler's process group is killed, any other process still
  running as the handler user is killed and reported, and files the handler user left in shared
  writable directories (`/tmp`, `/var/tmp`, `/dev/shm`, ...) are removed;
* environment: `AWS_ENDPOINT_URL`, `AWS_REGION`, credentials, and `CROSSREF_TABLE`.

So anything a handler needs to know later has to be in AWS (Parameter Store, DynamoDB, ...).
stderr of every request is kept in `$STACKCTL_HOME/logs/` (default `/var/lib/stackctl/logs/`).

## Bootstrap

`stackctl bootstrap` provisions the account-level resources once per environment: the DynamoDB table
named by `CROSSREF_TABLE` (default `cdk-crossref-tags`, partition key `name` of type `S`, on-demand).
That is all a fresh environment has; its key schema is fixed and shared by every app in the account.
