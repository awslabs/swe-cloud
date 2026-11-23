# Tenant Customer Directory — interface

The program is a long-running HTTP JSON server that keeps a customer (user)
directory for several **organizations** (tenants) in one PostgreSQL database.
Each organization is reached through an **application** whose `clientId` /
`clientSecret` pair is that organization's customer API credential. A credential
acts as administrator of its own organization only: its users, groups, roles,
uploaded files (resources, stored in Amazon S3), organization settings and the
application's OAuth 2.0 tokens.

## Invocation

```
program [-config PATH] [-createDatabase]
```

* `-config PATH` — configuration file (default `conf/app.conf`, relative to the
  current working directory).
* `-createDatabase` — before starting, connect to the server's `postgres`
  maintenance database and create the database named by `dbName` if it does not
  exist yet. Tables are created/migrated automatically at every start. Without
  this flag a missing database is a fatal start-up error (nonzero exit).

The server runs in the foreground until it is terminated (SIGTERM/SIGINT). Relative
paths in the configuration (the init-data file, session scratch directory) resolve
against the working directory, which must be writable. Start-up takes a few seconds;
poll an API URL until it answers before sending requests.

## Configuration file

INI-style `key = value` lines. A complete working configuration for the admitted
scope:

```ini
httpport = 18000
runmode = prod
copyrequestbody = true
driverName = postgres
dataSourceName = user=app password=app-pass-1 host=HOST port=PORT sslmode=disable dbname=directory
dbName = directory
origin = http://127.0.0.1:18000
ldapServerPort = 0
ldapsServerPort = 0
radiusServerPort = 0
logConfig = {"adapter":"console"}
initDataFile = ./init_data.json
initDataNewOnly = true
licenseServer = none
```

* `driverName` must be `postgres`. `dataSourceName` is a libpq-style key/value
  string (`host`, `port`, `user`, `password`, `dbname`, `sslmode=disable`);
  `dbname` and `dbName` name the same database.
* `httpport` is the listening TCP port (all interfaces); `origin` is the server's
  own base URL.
* Port `0` disables the auxiliary directory-protocol listeners, which are outside
  this task.
* `licenseServer = none` keeps the server fully offline.

### AWS / database setup

The database is an Amazon RDS for PostgreSQL instance provided by the AWS endpoint
in `AWS_ENDPOINT_URL` (region `us-east-1`, any credentials). Create an instance with
`Engine="postgres"`, wait for `db_instance_available`, and put the returned
`Endpoint.Address` / `Endpoint.Port` and master credentials into `dataSourceName`.
Use `-createDatabase` on first start (or create the database yourself). Delete the
instance when finished.

## Provisioning tenants (init-data file)

Organizations, applications/credentials and optional initial users are provisioned
at start-up from the JSON file named by `initDataFile`:

```json
{
  "organizations": [
    {"owner": "admin", "name": "acme", "displayName": "Acme",
     "passwordType": "bcrypt", "isProfilePublic": false,
     "countryCodes": ["US"], "languages": ["en"], "initScore": 0}
  ],
  "applications": [
    {"owner": "admin", "name": "app-acme", "displayName": "Acme app",
     "organization": "acme", "clientId": "cid-acme", "clientSecret": "sec-acme",
     "enablePassword": true, "redirectUris": [], "grantTypes": ["client_credentials"],
     "tokenFormat": "JWT", "expireInHours": 1}
  ],
  "users": [
    {"owner": "acme", "name": "seed-user", "type": "normal-user", "displayName": "Seed"}
  ]
}
```

Further optional arrays in the same file provision **providers** (storage
back ends, see *Resources*) and **certs** (token signing certificates, see *OAuth
tokens*). An application refers to them by name:

```json
{
  "providers": [
    {"owner": "admin", "name": "files", "displayName": "files", "category": "Storage",
     "type": "AWS S3", "clientId": "test", "clientSecret": "test", "regionId": "us-east-1",
     "bucket": "files_0a1b2c", "endpoint": "http://engines:4566",
     "domain": "", "pathPrefix": ""}
  ],
  "certs": [
    {"owner": "admin", "name": "cert-acme", "displayName": "Acme signing", "scope": "JWT",
     "type": "x509", "cryptoAlgorithm": "RS256", "bitSize": 2048, "expireInYears": 20,
     "certificate": "-----BEGIN CERTIFICATE-----\n...", "privateKey": "-----BEGIN RSA PRIVATE KEY-----\n..."}
  ],
  "applications": [
    {"...": "as above",
     "grantTypes": ["client_credentials", "password", "refresh_token"],
     "providers": [{"name": "files", "canSignUp": false, "canSignIn": false,
                    "canUnlink": false, "prompted": false, "alertType": "None"}],
     "cert": "cert-acme"}
  ]
}
```

Organization fields the tests set are `passwordType` (`bcrypt` or `plain`) and,
through the API, `passwordOptions` (see *Passwords*). An application's `grantTypes`
lists the OAuth grants its credential may use. An application without `cert` signs
with the server's own certificate `cert-built-in`, which the server creates (an
RSA 4096-bit key pair) on first start in an empty database.

Provision at least two organizations, each with its own application. Organization
and application owners are `admin`. Do not use the reserved organization
`built-in` for tenant fixtures — it is the server's own administrative
organization. With `initDataNewOnly = true`, objects already present in the
database are left untouched on later starts, so directory changes survive restarts;
provisioning still runs at every start and creates any listed object that is
missing.

## Authentication

Every `/api/` request below (except the OAuth endpoints, which take the credential as
described there) carries the application credential, either as HTTP Basic
authentication (`Authorization: Basic base64(clientId:clientSecret)`) or as query
parameters `clientId=...&clientSecret=...`. The credential is resolved to its
application and from there to the application's organization.

## Directory API (admitted scope)

User identifiers are `owner/name`, where `owner` is the organization name.

| Method | Path | Parameters / body |
|---|---|---|
| GET | `/api/get-users` | query `owner=ORG`; optional `p` (page, from 1) and `pageSize` |
| GET | `/api/get-user` | query `id=ORG/NAME` (an `owner` query parameter may also be present) |
| POST | `/api/add-user` | JSON user object with at least `owner` and `name` |
| POST | `/api/update-user` | query `id=ORG/NAME`, optional `owner`, optional `columns`; JSON body is the user object as returned by `get-user`, with the fields to change edited |
| POST | `/api/delete-user` | JSON body `{"owner": ORG, "name": NAME}` |

`columns` is a comma-separated list of JSON field names (for example
`columns=title,displayName`); when present, the update is restricted to those
fields. Request bodies are JSON (`Content-Type: application/json`).

Admitted user fields: `owner`, `name`, `displayName`, `firstName`, `lastName`,
`email`, `title`, `bio`, `affiliation`, `tag`, `homepage`, `location`, `region`,
`language`, `gender`, `birthday`, `education`, and the server-assigned `id` (a UUID
that identifies the record and must be sent back unchanged on update),
`createdTime` and `updatedTime` (RFC 3339 timestamps; a `createdTime` supplied when
adding a user is kept as the same instant). Also admitted: `avatar` (a URL, set by an
avatar upload, see *Resources*) and `groups` (a list of `ORG/GROUP` ids, edited
through update-user). `phone` may appear in requests and is validated, but phone
numbers are otherwise outside the graded scope. Responses contain many other user
properties; their values are outside the graded scope, except that secrets such as
`password` are reported in masked form. MFA, SAML, browser login pages and
authorization-code flows, the web UI and global administration are not part of this
task.

### Response envelope

Every response is a JSON object with a `status` of `"ok"` or `"error"`, a `msg`
string, and a `data` member (plus auxiliary members such as `data2`, which carries
the total count for paginated lists). Errors — including authorization failures —
are reported in this envelope; observe the HTTP status code and envelope rather than
assuming a particular HTTP error code. Messages are English.

## Organization

| Method | Path | Parameters / body |
|---|---|---|
| GET | `/api/get-organization` | query `id=admin/ORG` |
| POST | `/api/update-organization` | query `id=admin/ORG`; JSON body is the organization object as returned, edited |
| GET | `/api/get-user-count` | query `owner=ORG`, `isOnline` (empty for all users, `1` for online users) |
| GET | `/api/get-sorted-users` | query `owner=ORG`, `sorter` (a user column name), `limit` |

Admitted organization fields: `owner`, `name`, `displayName`, `websiteUrl`,
`passwordType`, `passwordOptions`, `countryCodes`, `languages`. Compared: replies to
reads and updates of the credential's own organization, attempts on another
organization, attempts to rename, the effect of updates on the stored row and on
later requests, and survival of edits across restarts.

## Passwords

| Method | Path | Parameters |
|---|---|---|
| POST | `/api/set-password` | query `userOwner=ORG`, `userName=NAME`, `newPassword=...` (no body) |

Passwords are also given in `add-user` / `update-user` bodies (`password`). The
organization's `passwordType` decides how the secret is stored (`plain` stores the
text as given). The organization's `passwordOptions` is a list of rule names; the
tests use `AtLeast8` and `Aa123`. Compared: replies (including the rule messages),
which operations apply the rules, whether stored rows change, and whether the user
can then sign in with the password through the OAuth password grant.

## Groups and roles

| Method | Path | Parameters / body |
|---|---|---|
| GET | `/api/get-groups` | query `owner=ORG`; optional `p`, `pageSize` |
| GET | `/api/get-group` | query `id=ORG/NAME` |
| POST | `/api/add-group` | JSON group object |
| POST | `/api/update-group` | query `id=ORG/NAME`; JSON body is the group as returned, edited |
| POST | `/api/delete-group` | JSON `{"owner": ORG, "name": NAME}` |
| GET | `/api/get-roles` | query `owner=ORG` |
| GET | `/api/get-role` | query `id=ORG/NAME` |
| POST | `/api/add-role` | JSON role object |
| POST | `/api/update-role` | query `id=ORG/NAME`; JSON body is the role as returned, edited |
| POST | `/api/delete-role` | JSON `{"owner": ORG, "name": NAME}` |

Admitted group fields: `owner`, `name`, `displayName`, `type` (`Virtual` or
`Physical`), `parentId` (the organization name for a top-level group, otherwise the
parent group's name), `isTopGroup`, `isEnabled`, `manager`, `contactEmail`. A user's
membership is its `groups` list; `/api/get-users` accepts `groupName=GROUP` to list a
group's members. Admitted role fields: `owner`, `name`, `displayName`,
`description`, `users`, `groups`, `roles` (`ORG/NAME` ids), `domains`, `isEnabled`.
Compared: replies, stored rows, renames (including references held by other
objects), deletions that are refused, and duplicates.

## Resources (files in Amazon S3)

An application's storage provider (category `Storage`, type `AWS S3`, owner `admin`
or the organization) names a bucket and the AWS endpoint. The tests create the bucket
(names like `files_0a1b2c`) on `http://engines:4566` before starting the server and
read objects back from it.

| Method | Path | Parameters / body |
|---|---|---|
| POST | `/api/upload-resource` | query `owner=ORG`, `user=NAME`, `application=APP`, `tag`, `fullFilePath`, optional `provider`, `parent`, `description`, `createdTime`; body `multipart/form-data` with one part named `file` |
| GET | `/api/get-resources` | query `owner=ORG`, `user=NAME`; optional `p`, `pageSize` |
| POST | `/api/delete-resource` | JSON resource object (at least `owner`, `name`) |

`tag` is `custom` for ordinary files (paths like `resource/ORG/NAME/file.ext`) and
`avatar` for a user's picture (paths like `avatar/ORG/NAME.png`). Admitted resource
fields: `owner`, `name` (the object key), `createdTime`, `user`, `provider`,
`application`, `tag`, `parent`, `fileName`, `fileType`, `fileFormat`, `fileSize`,
`url`, `description`. Compared: the reply (`data`, `data2`), the S3 objects (keys and
exact bytes), the resource records, which paths, file kinds and providers are
accepted, naming of repeated uploads, the provider's `pathPrefix` and `domain`, the
effect on the user's `avatar`, deletion of objects and records, and survival across
restarts.

## OAuth tokens and discovery

| Method | Path | Parameters |
|---|---|---|
| POST | `/api/login/oauth/access_token` | form fields `grant_type` (`client_credentials`, `password`, `refresh_token`), `client_id`, `client_secret` (or HTTP Basic), `username`, `password`, `refresh_token`, `scope` |
| POST | `/api/login/oauth/introspect` | form field `token` (optional `token_type_hint`); client credential as HTTP Basic |
| POST | `/api/login/oauth/revoke` | form fields `token`, `token_type_hint`, `client_id`, `client_secret` |
| GET | `/api/get-account`, `/api/userinfo` | `Authorization: Bearer ACCESS_TOKEN` |
| GET | `/api/get-tokens` | query `owner=admin`, `organization=ORG` (application credential) |
| GET | `/.well-known/openid-configuration`, `/.well-known/ORG/openid-configuration` | anonymous |
| GET | `/.well-known/jwks`, `/.well-known/ORG/jwks` | anonymous |

Tokens are RS256 JWTs signed with the application's certificate; the header's `kid`
is the certificate name and the JWKS lists every certificate's public key. These
endpoints do not use the response envelope: a grant answers with a token object,
failures with HTTP 400/401 and `{"error", "error_description"}`, introspection with
an object holding `active`, revocation with an empty body, and the discovery and JWKS
documents are bare JSON. Compared: HTTP status and the decoded body (exact error
descriptions), token claims (`owner`, `name`, `id`, `type`, `tokenType`, `sub`,
`aud`, `azp`, `iss`, `scope`, profile claims, and `exp - iat`), that signatures verify
with the published key, the reply keys of each grant, failed password attempts and
lockout, refresh and revocation effects, discovery document URLs (built from
`origin`) and advertised values, JWKS key members (`kid`, `kty`, `alg`, `use`, `n`,
`e`, `x5c`) and their survival across restarts. Token objects from `get-tokens` are compared by
`application`, `organization`, `user`, `expiresIn` and `tokenType`.

## Storage

Users are stored in table `"user"` with primary key `(owner, name)`; column names
are the snake_case forms of the JSON field names (e.g. `display_name`,
`created_time`), and the admitted fields are stored as text holding the same string
the API reports. Applications are stored in table `application` (`name`,
`organization`, `client_id`, `client_secret`) and organizations in `organization`
(`owner`, `name`); the application table is the authoritative record of
credentials. Organization columns compared also include `display_name`,
`website_url`, `password_type` and `password_options` (JSON text). User columns
compared also include `avatar`, `groups` (JSON text) and `password` (never the plain
text unless the organization's `passwordType` is `plain`). Groups are stored in table
`"group"` (`owner`, `name`, `display_name`, `type`, `parent_id`, `is_top_group`,
`is_enabled`, `manager`, `contact_email`), roles in `role` (`owner`, `name`,
`display_name`, `description`, `users`, `groups`, `roles` as compact JSON text,
`is_enabled`), resources in `resource` (complete rows), and certificates in `cert`
(`owner`, `name`, `bit_size`). A Postgres client library (`psycopg`) is available in
the environment.

## How behavior is compared

Tests start `/app/program` in a fresh working directory with a configuration and
init-data file like the ones above (each test uses its own database on a shared RDS
instance), wait up to 45 seconds for the port to answer, send HTTP requests, and
inspect the database directly. A test allows 60 seconds in total (150 seconds for
tests that restart the server), so start-up should take a few seconds at most.
They compare:

* the HTTP status code and the decoded JSON envelope — `status`, `msg` (exact
  text), `data`, and `data2` for paginated lists. Whitespace, key order and extra
  envelope members are not compared;
* in user objects, `owner`, `name`, every admitted profile field (exact strings;
  an unset field is `""`), `id`, `createdTime`/`updatedTime` and the masked
  `password`. Other properties are ignored;
* lists as collections: element order is not compared, but page membership is.
  Users that share a `createdTime` are never split across a page boundary;
* counts by numeric value; timestamps as instants (any RFC 3339 offset);
* S3 objects by key and exact bytes;
* stored rows: admitted columns of `"user"`, and complete `"user"` rows before and
  after operations that must not change them (a refused or failed request must
  leave every stored row exactly as it was); application/organization rows by the
  columns named above.
