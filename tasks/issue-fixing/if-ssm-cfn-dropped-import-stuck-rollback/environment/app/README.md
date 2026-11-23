# storefront platform: cross-stack exports

The storefront app is two stacks: **SharedNetwork** (VPC, load-balancer security groups, edge
certificate) and **Storefront** (the web tier), which references SharedNetwork's values through
CDK cross-region references. This repository holds what deploys them in this environment:

```
crossref/        custom-resource handlers: writer.py (producer side), reader.py (consumer side),
                 tagstore.py (SSM tags kept in DynamoDB), entrypoint.py (request/response plumbing)
stackctl/        local stand-in for CloudFormation (docs/stack-engine.md)
infra/rev-NN/    synthesized templates of each app revision, with the stack manifest
docs/            cross-stack-references.md (how the exports work), stack-engine.md (engine contract)
```

AWS is Floci on `http://localhost:4566` (us-east-1, credentials `test`/`test`, already set in the
environment).

## Running

```bash
cd /app
python3 -m stackctl bootstrap              # once per environment
python3 -m stackctl deploy infra/rev-41    # SharedNetwork, then Storefront
python3 -m stackctl deploy infra/rev-42
python3 -m stackctl status
python3 -m stackctl events Storefront -n 50
ls /var/lib/stackctl/logs/                 # stderr of every handler request
python3 -m stackctl reset                  # forget local stacks, wipe /cdk/exports and the tag table
```

Each deploy publishes `crossref/` as the stacks' handler code (`$STACKCTL_HOME/assets/`, see
docs/stack-engine.md, *Functions*): a stack runs the handlers it was last deployed with, so a change to
`crossref/` reaches a stack with that stack's next deploy.

`aws --endpoint-url $AWS_ENDPOINT_URL ssm get-parameters-by-path --path /cdk/exports --recursive`
shows the exports.
