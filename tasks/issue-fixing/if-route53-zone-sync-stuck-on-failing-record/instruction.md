# DNS reconciliation stopped converging in example.test.

You own the `cluster-a` DNS reconciler for the `example.test.` hosted zone. It
runs as a one-shot cycle: read the zone, compare it with the desired endpoint
list, submit whatever is missing or wrong, report. Since the last release it has
stopped keeping the zone in shape, and three separate complaints are open against
it.

## What was seen

**The zone froze.** A cycle against a zone that was well behind produced:

```
$ python3 /app/reconciler.py once --endpoint-url http://127.0.0.1:4567 \
      --zone-id ZEXAMPLE --owner-id cluster-a --desired /app/desired.json \
      --max-changes-per-call 8
change batch rejected: An error occurred (InvalidChangeBatch) when calling the
ChangeResourceRecordSets operation: FATAL problem: the change batch carries 32
changes and this endpoint accepts at most 8 changes per ChangeResourceRecordSets
request
{"applied": [], "failed": ["gone.example.test.", "svc-00.example.test.",
 "svc-01.example.test.", "svc-02.example.test.", "svc-03.example.test.",
 "svc-04.example.test.", "svc-05.example.test.", "svc-07.example.test.", ...]}
```

Nothing landed. The same report came back on every later cycle. The zone is
still serving stale addresses for every endpoint that was not already correct.

**A deleted record never came back.** During a drill somebody removed the A
record for one of our names directly in the console and left everything else
alone. The claim record for that name is still in the zone, exactly as it was,
and the record has not returned across many cycles. The cycle log from that
period repeats:

```
InvalidChangeBatch: [Tried to create resource record set
[name='a-svc-07.example.test.', type='TXT'] but it already exists]
```

**The report stopped accounting.** The third complaint is about the report. A
name appears under `failed` on the run where its change is refused and is never
mentioned again while it is still not serving, and the names this tool will not
write at all have never appeared in one. On-call can no longer tell from a
report which endpoints are in shape and which are not, only which ones a given
cycle happened to try. From the last drill's handover: "svc-11 came back failed
on Monday and has not been mentioned since. It is still serving the wrong
address."

## The zone is shared

`example.test.` is not ours alone. A second controller runs as `cluster-b`, and
its claim records sit in the zone alongside ours. Operations staff have also
seen A records appear under our `svc-*` names carrying `192.0.2.x` addresses we
never asked for, and disappear again shortly afterwards. Treat any name in the
zone as something another writer may be holding at the moment your change
arrives, whatever the listing you took a moment earlier said. Those writers do
not give up after one try: the same name can be taken again between every
listing and every change you send, cycle after cycle. Their A records carry no
claim, so they never change whose name it is; the claim records decide that.

## The endpoint in front of Route 53

All Route 53 traffic goes through the operator relay on
`http://127.0.0.1:4567`. It forwards requests unchanged, refuses any
`ChangeResourceRecordSets` request carrying more than **8 changes**, and
otherwise behaves as Route 53 does. Point `--endpoint-url` at it. Route 53
applies one `ChangeResourceRecordSets` request as a unit: every change in the
batch lands, or none of them does.

## What must hold afterwards

Ownership is carried by claim records: data record `svc-07.example.test.` is
claimed by TXT `a-svc-07.example.test.`, whose value is a quoted label list
beginning `heritage=external-dns` and carrying `external-dns/owner=<id>`. A name
is ours while its claim names `cluster-a`.

1. A data record and the claim on it are put into the zone, and taken out of
   it, by the **same** `ChangeResourceRecordSets` request. A claim of ours must
   never be left sitting in the zone without its record.
2. A name whose claim names another owner is not ours. We do not create,
   overwrite, delete or claim it, and we do not write a record under it.
3. A name that is being served but carries no claim, or a claim we cannot
   parse, is not ours either. We leave it alone.
4. A claim that already exists is never rewritten. When a delete has to name a
   claim value, it names the value actually stored there.
5. A name of ours whose record went missing is restored under the claim that is
   still there, with that claim untouched, byte for byte.
6. A name of ours that is no longer desired gives up both its record and its
   claim.
7. An endpoint that cannot be committed this cycle must not stop any other
   endpoint from converging. After **two** cycles, every endpoint that can
   converge must have converged, and every endpoint that still has not --
   including one that is not ours to touch -- must be reported under `failed`
   rather than swallowed.

Keep the tool's contract: the same command line, one JSON line
`{"applied": [...], "failed": [...]}` on stdout that accounts for every
endpoint on the desired list -- those that are in shape, serving what the list
asks of them under a claim of ours, under `applied`, and those that are not,
whatever the reason, under `failed` -- exit 0 when every desired endpoint is in
shape, 2 when some are not, and no state carried between invocations -- each
cycle is a fresh process in a fresh working directory that knows only its
arguments and the zone. The relay, the emulator and the zone are not yours to
change.

## How to run it

```
# a scratch zone of your own, through the relay
aws --endpoint-url http://127.0.0.1:4567 route53 create-hosted-zone \
    --name example.test. --caller-reference scratch-1

# one cycle
python3 /app/reconciler.py once --endpoint-url http://127.0.0.1:4567 \
    --zone-id <zone-id> --owner-id cluster-a --desired /app/desired.json \
    --max-changes-per-call 8
```

`/app/desired.json` is the endpoint list; `/app/relay.py` is the relay. The
graded run prepares its own zone and desired list, runs three cycles of the
reconciler as separate processes, and reads the zone and the reports back
afterwards, so fix the tool and not the zone.
