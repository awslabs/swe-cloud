# dns-desired-state: configuration language

`dnsconfig.js` is an ordinary JavaScript program. The program evaluates it with
a set of predefined functions; their calls declare zones and records. Ordinary
JavaScript (variables, loops, string operations, arrays, `function`
definitions) may be used to build the arguments.

This guide lists the admitted functions and their syntax. How a declaration
is normalised, validated and reconciled with an existing zone is the
behaviour to be observed by running the program.

## Top level

```javascript
var REG_NONE = NewRegistrar("none");      // registrar that does nothing
var R53 = NewDnsProvider("r53");          // key in creds.json (TYPE ROUTE53)

D("example.test", REG_NONE, DnsProvider(R53),
  DefaultTTL(600),
  A("@", "192.0.2.1"),
  // ... more records and domain modifiers ...
);
```

* `NewRegistrar(name)` returns a registrar handle. Only `"none"` is in scope.
* `NewDnsProvider(name)` returns a DNS-provider handle for the `creds.json`
  entry `name`.
* `D(zoneName, registrar, modifiers...)` declares one zone. Every following
  argument is a domain modifier: `DnsProvider(...)`, a record function, or one
  of the modifiers below. Arguments may also be (nested) arrays of modifiers.
  A zone name may be declared by only one `D()` call.
* `DnsProvider(handle)` attaches the provider to the zone.

## Records

`name` is a label relative to the zone (`"www"`, `"_sip._tcp"`), or `"@"`
for the zone apex. Target host names are either relative labels or fully
qualified names ending with `.`; see "Names and the trailing dot" below.

| Function | Arguments |
|----------|-----------|
| `A(name, ipv4, mods...)` | IPv4 address string. |
| `AAAA(name, ipv6, mods...)` | IPv6 address string. |
| `CNAME(name, target, mods...)` | Target host name. |
| `MX(name, priority, target, mods...)` | Integer preference and mail host. |
| `TXT(name, contents, mods...)` | A string, or an array of strings. Write the plain text; DNS quoting and splitting are handled by the program. |
| `SRV(name, priority, weight, port, target, mods...)` | Three integers and a target host. |
| `CAA(name, tag, value, mods...)` | `tag` is `issue`, `issuewild`, `iodef`, ...; `value` is the plain value. |
| `R53_ALIAS(name, type, target, mods...)` | Route 53 alias record set of `type` (`A`, `AAAA`, `CNAME`, ...) pointing at `target`. |

Several calls with the same `name` and type form one record set.

### Record modifiers

* `TTL(seconds)` sets the TTL of that one record. Values such as `"5m"`,
  `"1h"`, `"1d"` are also accepted.
* `R53_ZONE(zoneId)` on `R53_ALIAS` names the hosted zone that contains the
  alias target (needed for targets outside the current zone, e.g. an AWS
  load balancer). Without it the alias is taken to point into the current
  zone, whose id is filled in automatically.
* `R53_EVALUATE_TARGET_HEALTH(true|false)` on `R53_ALIAS` sets the alias
  target's health-evaluation flag.

## Domain modifiers

* `DefaultTTL(seconds)` — TTL for records in this `D()` that do not carry
  `TTL()`. It applies to records declared after it, so place it first.
  Without it a built-in default is used.
* `NO_PURGE` (no parentheses) — record sets with no declared record of the
  same name and type are left in place. Declared sets are reconciled to exactly
  their declared values, including removal of extra members; records are still
  added and updated.
* `IGNORE(labelPattern, typePattern, targetPattern)` — existing records that
  match are neither modified nor deleted. Patterns are glob patterns (`*`,
  `?`, `{a,b}`); `labelPattern` matches the relative label (`@` for the
  apex); `typePattern` is a comma-separated list of record types or `*`;
  `targetPattern` matches the record target. Omitted patterns mean `*`.
* `IGNORE_NAME(labelPattern)` is `IGNORE(labelPattern, "*", "*")`;
  `IGNORE_NAME(labelPattern, types)` is `IGNORE(labelPattern, types, "*")`.

Declaring records that an `IGNORE` pattern also matches is treated as unsafe
when the zone is compared with its live contents.

## Names and the trailing dot

A host-name argument that ends with `.` is fully qualified and used as is
(`"mail.example.net."`). A name that does not end with `.` is relative to the
current zone (`"www"` in zone `example.test` means `www.example.test.`). A
relative target that contains a dot (`"foo.bar"`) is rejected as ambiguous for
most record types: write it either fully qualified with the final dot or as
a single label. Record names (the first argument) are always relative; `"@"`
means the apex.

## Errors

Evaluation errors (JavaScript exceptions, unknown functions) and validation
errors (malformed addresses, invalid names, conflicting records, wrong
argument types) make `check`, `preview` and `push` exit with a non-zero
status before any provider change is made.

## Variables and multiple files

`-v name=value` on the command line defines a global JavaScript string
variable `name` before the file is evaluated. `require("other.js")` loads
another local file relative to the including file. Remote files are out of
scope.
