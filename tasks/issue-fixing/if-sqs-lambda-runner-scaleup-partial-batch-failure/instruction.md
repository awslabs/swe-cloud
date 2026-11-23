# Scale-up still loses jobs and doubles runners when EC2 browns out

Our runner control plane's scale-up function is in `/app` (start with `/app/README.md`). It is our
Python port of the upstream terraform-aws-github-runner scale-up Lambda. Last month we moved to the
port of the release that hands the batch back to SQS when the handler hits an unexpected error. That
was meant to stop jobs vanishing during AWS trouble (upstream #5105). Tuesday's incident shows it
did not.

From the incident notes:

- For about 25 minutes the fleet API answered EC2 errors intermittently, mostly
  `503 Unavailable` and some `RequestLimitExceeded`. Parameter Store throttled too
  (`ThrottlingException: Rate exceeded` on `PutParameter`). The code host's API had a few 503s.
- Some jobs stayed queued until the code host cancelled them. No runner was ever configured for them, and
  their messages were gone from the build queue, not in the DLQ.
- Other jobs got two runners. `octo-web` filled its runner cap with duplicates, and one of its jobs
  kept bouncing off the cap until its message ended up in the DLQ.
- When the queue backlog drained afterwards, `initech` ran six runner instances at once against
  its cap of four.
- Afterwards we found runner instances with no config in Parameter Store. They had sat idle,
  holding cap slots, until scale-down reaped them. The function is supposed to terminate instances
  it could not configure.

Fix the scale-up function. A failure partway through a batch must not lose a job or give a job a
second runner, and the function must not leave unconfigured instances behind. The runner cap is a
hard limit. Setting `RUNNERS_MAXIMUM_COUNT=-1`, the workaround people use upstream, is not an
option.

Keep the deployment as it is: the queue, redrive policy, timeouts, event source mapping, the
function's IAM role, endpoints and the way the function is invoked (`python3 -m control_plane.runtime
control_plane.lambda_handler.scale_up_handler`, event on stdin, response on stdout). Events from
repositories not owned by an organisation are still ignored.

The local stack in `/app/devstack` runs the whole path against Floci. Its fleet and code-host APIs
never fail.
