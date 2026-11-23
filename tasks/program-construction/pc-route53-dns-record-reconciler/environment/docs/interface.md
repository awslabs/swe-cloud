# dns-desired-state: command-line interface

`dns-desired-state` compiles a JavaScript description of the DNS records a set
of zones *should* contain (the desired state), compares it with what an Amazon
Route 53 account *does* contain, and reports or applies the differences.

The program is a single executable, called `program` below. It has no other
runtime files: all JavaScript helpers that make up the DSL are built in.

## Inputs

Two files are read from the current directory unless overridden:

| File | Flag | Purpose |
|------|------|---------|
| `dnsconfig.js` | `--config FILE` | Desired state, written in the DSL described in [dsl.md](dsl.md). |
| `creds.json`   | `--creds FILE`  | Named provider credentials. |

A minimal `creds.json` for the Route 53 provider:

```json
{
  "r53": { "TYPE": "ROUTE53", "KeyId": "test", "SecretKey": "test" }
}
```

The key (`r53` here) is the name passed to `NewDnsProvider("r53")` in
`dnsconfig.js`; it is also the first argument of `get-zones`. `TYPE` must be
`ROUTE53`. `KeyId`/`SecretKey` are static AWS credentials. Whenever a graded
run provides a credentials file, it is exactly the file shown above (possibly
under another name via `--creds`). Graded runs also include failure cases: a
credentials file that does not exist at the given or default path, and a
`NewDnsProvider(name)` whose `name` has no entry in the file. The program is
expected to fail in these cases (non-zero exit, no Route 53 change) rather than
fall back to credentials from the environment.

## AWS endpoint

The program uses the standard AWS SDK configuration chain, so the
`AWS_ENDPOINT_URL` environment variable redirects every Route 53 call to an
emulator, e.g. `AWS_ENDPOINT_URL=http://engines:4566`. No other configuration
is required. The Route 53 API calls involved are the ordinary hosted-zone and
resource-record-set operations (list zones, get zone, list record sets, change
record sets).

Hosted zones are identified by name. The zones named in `dnsconfig.js` must
already exist in the account (create them with the Route 53 API); this task
does not cover zone creation. Use distinct zone names: when several hosted
zones share one name, which of them is selected is not part of the admitted
scope.

## Commands in scope

```
program [global options] preview  [options]
program [global options] push     [options]
program [global options] check    [--config FILE]
program [global options] get-zones [options] CREDKEY ZONE...|all
```

* `preview` evaluates the configuration, reads the live zones and prints the
  corrections that would be made. It never modifies Route 53.
* `push` does the same and then executes the corrections.
* `check` evaluates and validates `dnsconfig.js` without contacting any
  provider.
* `get-zones r53 ZONE...` (or `all`) reads existing zones from the provider
  and prints them. The older form `get-zones r53 ROUTE53 ZONE...|all`, with
  the provider type as second argument, is still accepted and additionally
  prints a deprecation warning on standard error; `--format` selects `js` (a `dnsconfig.js`
  skeleton), `djs` (same, leading-comma style), `zone` (BIND zone file), `tsv`
  or `nameonly`. `--creds` and `--out FILE` work as for the other commands.

Options of `preview` and `push` that are in scope:

| Option | Meaning |
|--------|---------|
| `--config FILE` | DSL file (default `dnsconfig.js`). |
| `--creds FILE` | Credentials file (default `creds.json`). |
| `--domains LIST` | Comma-separated list of zone names to process; other `D()` blocks are not compared or changed. A leading `*` wildcard (`*.example.com`) is accepted. |
| `--expect-no-changes` | Exit non-zero when any correction is found. |
| `-v name=value`, `--variable name=value` | Define a JavaScript variable before evaluation (repeatable). |
| `--no-colors` (global, before the command) | Disable ANSI colour. The graded runs always pass it. |

Other flags printed by `--help` exist but are outside the admitted scope
(`--full`, notifications, JSON reports, `--ir`, concurrency tuning, zone
auto-creation, `print-ir`, `fmt`, `init`, ...). Graded runs never use them.
Every zone declared with `D()` in a graded `preview`/`push` configuration
exists in the account. A zone name that does not exist may still be graded
in two places: as a `--domains` entry (it matches no `D()` block) and as a
`get-zones` argument (expected to fail).

## Observable results and how they are compared

The graded runs invoke the program as `/app/program --no-colors COMMAND ...`
with standard input closed, in a working directory holding the files, with
`AWS_ENDPOINT_URL` pointing at the emulator. One exception: `check` is also
run with `AWS_ENDPOINT_URL` pointing at an address where nothing listens, to
confirm it contacts no provider; `preview` in that setting is expected to
fail. Only the following is compared.

* **Exit status**: zero versus non-zero only. Non-zero is expected when the
  configuration fails to evaluate or validate, when a provider or zone fails,
  and when `--expect-no-changes` is given and corrections exist.
* **Route 53 state**, read independently with the Route 53 API after each
  command: for every zone involved, the complete set of record sets, keyed by
  (name, type). Every record set present must be expected and vice versa,
  including the apex `SOA` and `NS` sets that Route 53 creates with the zone.
  An ordinary set compares its TTL (an integer) and the multiset of its
  `ResourceRecords` values; the order of values inside a set is not
  significant, but each value is compared as the exact string stored (for
  example `"say \"hi\""` with its DNS quoting, `10 mx.example.net.`,
  `0 issue "ca.example"`). An alias set compares `AliasTarget.DNSName`
  (exact string), `HostedZoneId` and the boolean `EvaluateTargetHealth`.
  Names are compared exactly as stored; a missing final dot on a stored name
  or alias target is not significant.
* **Standard output of `preview`/`push`**: only two kinds of line are parsed.
  The summary line `Done. N corrections.` (exactly this form, also for N = 1,
  printed exactly once) gives the total number of corrections over all zones
  processed. A header line `******************** Domain: NAME` (20 asterisks)
  is printed for each zone that is processed. Everything else (per-zone counts,
  the wording of individual correction lines, informational and error text)
  is informational, as is standard error.
* **`get-zones` output**: compared line by line as a multiset (line order is
  not significant), after removing leading/trailing white space, blank lines
  and any `SOA` line or `//SOA(...)` comment. For `js`/`djs` a separator
  comma at the start (`, `) or end of a line and a final `;` are also
  removed before comparing, so the two styles must contain the same lines;
  `djs` additionally puts the comma at the start of every line inside
  `D(...)`. The `zone` format is compared as records: owner, TTL (explicit or
  from `$TTL`), type and data with runs of white space collapsed; a line
  starting with white space continues the previous owner; the first line
  must be `$ORIGIN ZONE.` (the zone name with a final dot). `tsv` and
  `nameonly` lines are compared exactly. The deprecated form only needs some
  warning text on standard error and the same standard-output lines as the
  current form (compared by the same rule).

The way changes are counted, which existing records are considered equal to
declared ones, and how failures in one zone affect others are part of the
behaviour to reconstruct.

## Example

```sh
export AWS_ENDPOINT_URL=http://engines:4566 AWS_REGION=us-east-1
python3 -c "import boto3; boto3.client('route53').create_hosted_zone(Name='example.test', CallerReference='r1')"
cat > creds.json <<'EOF'
{ "r53": { "TYPE": "ROUTE53", "KeyId": "test", "SecretKey": "test" } }
EOF
cat > dnsconfig.js <<'EOF'
var REG_NONE = NewRegistrar("none");
var R53 = NewDnsProvider("r53");
D("example.test", REG_NONE, DnsProvider(R53),
  A("www", "192.0.2.10"),
  MX("@", 10, "mail.example.net.")
);
EOF
/reference/program preview          # lists corrections, ends with "Done. N corrections."
/reference/program push             # applies them
/reference/program preview --expect-no-changes   # exit 0 once in sync
/reference/program get-zones --format=js r53 example.test
/reference/program get-zones --format=tsv r53 all
```
