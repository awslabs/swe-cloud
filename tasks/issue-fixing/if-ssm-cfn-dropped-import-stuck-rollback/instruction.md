# Storefront rev-42 can't be deployed: SharedNetwork stuck in UPDATE_ROLLBACK_FAILED

rev-42 moves Storefront off the legacy ALB security group onto the new one and drops the legacy
orders queue and topic. Deploying it the normal way (`python3 -m stackctl deploy infra/rev-42`:
SharedNetwork first, then Storefront, as the manifest orders them) fails on SharedNetwork's export
writer with `Exports cannot be updated: ... is in use by stack(s) Storefront`. The rollback then
fails the same way and leaves SharedNetwork in `UPDATE_ROLLBACK_FAILED`; in production that took
hand-editing Parameter Store and a continue-update-rollback to get out of. Deploying Storefront
first doesn't work either: it can't resolve the new security group's parameter, which doesn't
exist yet.

This isn't specific to rev-42 — any change that drops or swaps a cross-stack import runs into it —
so it has to be fixed in the export handlers in `/app/crossref`, not by deploying in a special
order.

Everything runs locally: Floci is already up, `/app/README.md` shows how to bootstrap and deploy
rev-41 and then rev-42 to reproduce it. `docs/stack-engine.md` is the contract of the engine that
stands in for CloudFormation; `docs/cross-stack-references.md` describes the export mechanism.

What has to be true after the fix:

- Dropping or swapping imports deploys in the normal order (producer, then consumer), and also in
  the order people used as a workaround (the consumer deployed exclusively, then the producer).
- The strong reference stays a guarantee. As long as a consumer stack uses an export — and that
  includes a consumer update that is still in progress or is rolling back — the parameter exists
  with the value the consumer resolved. A producer change to the value of an export that a consumer
  uses is still refused.
- The export handlers never leave a stack in `UPDATE_ROLLBACK_FAILED`. A producer update that fails,
  whether the writer refused it or another resource in the stack failed, rolls back to
  `UPDATE_ROLLBACK_COMPLETE`.
- Nothing piles up in Parameter Store. A parameter that no stack exports or imports any more is
  deleted by the time the deploy that left it in that state has finished. That covers dropped
  exports and exports created by a producer update that then rolled back. We're not adding a
  cleanup job: handlers only run while the engine waits on them.

Keep the handler entry points (`python3 -m crossref.writer`, `python3 -m crossref.reader`), the
request/response contract, parameter names and values, and the bootstrap table's key schema as they
are; the engine (`/app/stackctl`) and the templates are not yours to change, and the fix has to work
on the engine as documented. Anything inside `/app/crossref` is fair game.

The fix rolls out to environments that run rev-41 today, deployed by the current handlers. Nobody
re-bootstraps or redeploys them first: the handlers ship inside each stack (a `Local::Function`, see
`docs/stack-engine.md`), so a stack gets the new code with its own next deploy, the one that carries
rev-42. The first requests the new handlers see are that deploy's, against the stacks, parameters and
tag records exactly as the current handlers left them, and everything above has to hold for those
environments too. One case is out of scope: a first deploy that removes a stack's writer or reader
from the template altogether (a consumer dropping its last import, say). The engine sends that
Delete to the code the stack already runs, so the new handlers never get a say; every stack keeps
its handler through rev-42, and we'll handle the rare full removal by hand.
