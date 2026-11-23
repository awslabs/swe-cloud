# strata

Versioned dataset manifests on S3, with concurrent writers coordinated through a DynamoDB
version index.

```
stratactl              CLI entry point (python3 /app/stratactl ...)
strata/cli.py          argument parsing, JSON output, exit codes
strata/handler.py      commit and resolve protocol
strata/index.py        DynamoDB version index
strata/objects.py      S3 object store (versioned bucket)
strata/paths.py        manifest naming
strata/aws.py          boto3 clients (endpoint from AWS_ENDPOINT_URL)
scripts/bootstrap.py   create the table and the versioned bucket
docs/PROTOCOL.md       commit and reader protocol, table layout
docs/OPERATIONS.md     who runs what, environment, how to run it locally
ops/                   evidence captured from production incidents
```

Configuration: `AWS_ENDPOINT_URL`, `STRATA_TABLE` (default `strata-manifests`), `STRATA_BUCKET`
(default `strata-warehouse`), `STRATA_LOG_LEVEL`.
