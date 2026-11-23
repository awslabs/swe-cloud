# Rebuild a multi-tenant customer directory server

`/reference/program` is an HTTP JSON server that keeps a customer (user) directory
for several organizations in one Amazon RDS for PostgreSQL database. Each
organization has an application whose `clientId` / `clientSecret` is that
organization's API credential; a credential administers its own organization's
users, groups, roles, settings, uploaded files (kept in Amazon S3) and OAuth tokens,
and nothing else. Write your own implementation that behaves like it, and
make `/app/build.sh` produce the executable `/app/program`.

Start with `/docs/interface.md`: invocation, configuration and init-data files, RDS
setup through `AWS_ENDPOINT_URL`, authentication, the directory, organization,
password, group, role and resource endpoints, S3 storage providers, the OAuth token,
introspection and discovery endpoints, admitted fields, storage tables and how
behavior is compared. Then run the
reference against your own RDS instance and study what it returns and stores,
especially:

- responses for success and every kind of failure, word for word;
- which tenant identifier (`id`, an `owner` query parameter, body fields) decides
  access, and which record an operation actually touches;
- update semantics: omitted fields, `columns`, renames, identity and uniqueness;
- what survives restarts and how provisioning treats existing data;
- same-named users in different organizations, which stay independent rows;
- which uploads are accepted, where their bytes land in S3 and how repeats are named;
- token contents, signatures, grant failures and what refresh and revocation change.

Refused and failed requests must not change any stored row.

Use the reference only by running it; do not copy or decompile it. Only `/app`
carries over to grading; the reference, its database and your AWS resources do
not. Any installed language is fine; Python 3.12 with `boto3` and `psycopg` is
available, and its standard library suffices for the HTTP server. MFA, SAML,
browser login pages, the web UI and global administration are out of scope.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
