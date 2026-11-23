# Cross-stack references through Parameter Store

Our apps use CDK's `crossRegionReferences`. When a consumer stack references a value owned by a
producer stack, CDK does not use CloudFormation exports; it synthesises two custom resources and an
SSM parameter instead. This repository carries our port of the two handlers (`crossref/`), run by
the local stack engine (`docs/stack-engine.md`). Upstream design: aws-cdk
`packages/aws-cdk-lib/core/adr/cross-region-stack-references.md`.

## The pieces

* **One parameter per exported value per consumer**: `/cdk/exports/<ConsumerStack>/<id>`, a String
  parameter. The id is derived from the producer stack, its region and the *token* being
  referenced (e.g. `SharedNetworkuseast1FnGetAttAlbSgV2...GroupId...`), not from its value: if the
  producer replaces the resource, the parameter name stays and its value changes.
* **`Custom::CrossRegionExportWriter`** in the producer stack (`crossref.writer`). Its properties
  are `WriterProps: {region, exports: {parameterName: value}}` — exactly the set of references the
  consumers make at synth time. Removing a reference from a consumer removes the export from the
  producer in the same app revision.
* **`Custom::CrossRegionExportReader`** in the consumer stack (`crossref.reader`). Its properties
  are `ReaderProps: {region, prefix: <consumer stack name>, imports: {parameterName:
  "{{resolve:ssm:<parameterName>}}"}}`, so it receives the resolved values; it returns them as
  `Data`. The consumer's own resources reference the same parameters with dynamic references.
* Each of the two handlers is deployed with the stack that uses it, as a `Local::Function` whose code
  is the `crossref/` directory (CDK's custom-resource provider, one per stack). A stack runs the
  handler code of its last deploy.
* CDK makes the consumer depend on the producer, so a normal deploy updates the producer first.
  A consumer cannot be deployed before a parameter it newly imports exists: resolution fails.

## Strong references

Native CloudFormation exports refuse to change or delete an export another stack imports. These
references keep the same guarantee, because without it a consumer can be broken by a producer it
does not control:

```
deploy 1   producer: create export E=v1          consumer: resolve E, create resources
deploy 2   producer: delete E, create E'=v2      consumer: resolve E', update fails -> rollback
           consumer rollback: resolve E -> "cannot be found" -> rollback fails. Stuck.
```

So: the writer may create new exports, but may only change or delete an existing export while no
consumer imports it. The reader records that a consumer imports a parameter (upstream: the tag
`aws-cdk:strong-ref:<ConsumerStack>` on the parameter) and removes that record when the import goes
away; the writer checks for it and fails the producer update instead of breaking the consumer. Our
emulator has no SSM tagging, so the tags live in DynamoDB (`crossref/tagstore.py`, table
`CROSSREF_TABLE`); the semantics are SSM's.

## What custom resources can and cannot see

* A custom resource runs only when its properties change. There is no hook that runs on every
  deploy.
* It sees the properties of the current update and of the previous one (`OldResourceProperties`),
  nothing older. Anything else it needs to know it has to keep in some external state.
* On a failed update CloudFormation rolls back by sending the swapped Update (or a Delete for a
  replacement); the handler sees a request that looks like an ordinary update in the other
  direction.

## Notes from operations

* Leftover parameters (aws-cdk #37845): a parameter can exist without a writer that owns it, e.g.
  after a stack was deleted with the writer's Delete abandoned. A new writer must take it over, so
  `put_parameters` writes with `Overwrite=True` (upstream #37852).
* `DeleteParameters` accepts at most 10 names per call; deletions are chunked.
