# Incident: tile log committed checkpoints that do not survive an instance swap

You are on call for the tile log service. The service is
`/app/sequencer.py` (contract and run book in `/app/README.md`): an
append-only Merkle tree log whose committed head is a checkpoint record
in DynamoDB, whose published head is the `checkpoint` object in S3, and
whose entries are readable as tile objects at fixed public
coordinates. Clients submit entries over HTTP and may treat them as
part of the log only once the submission has been answered as
committed.

## What was observed

Last Tuesday's failover drill was run with a configuration mistake: the
primary was killed mid-commit while a duplicate instance was still
running, and both instances had accepted one submission each from the
same starting checkpoint. The postmortem found, in order:

1. **An unverifiable committed tree.** The record said the log stood at
   a size with root `X`. When the auditors recomputed the root from the
   published tile objects at the public coordinates, they got `Y != X`.
   The entry bytes published for those indexes were not the bytes the
   committed root was computed from. The record itself never moved
   after the winner's write; only the objects disagreed with it.

2. **A published head that lagged for minutes.** In a second run of
   the same drill, killed at a different point, the replacement process
   came up and reported healthy, but the published `checkpoint` object
   still described the empty tree. Readers resolving the committed
   entries found nothing there. The published head only caught up when
   a later submission happened to be sequenced — nothing else could
   have caught it up: the submission the primary was holding died with
   it, the client had been told nothing, and the replacement came up in
   an empty directory with nothing but the services.

3. **An abandoned attempt in the way.** In a third run, the primary was
   killed before its commit reached the record. Its attempt left
   objects behind at the coordinates its entries would occupy. The
   replacement had to place different entries at those same
   coordinates, and the drill operators had to judge whether that was
   safe.

## What must hold afterwards

The drill is going back on the schedule, run the same way it was run
Tuesday: a primary killed in the middle of committing, a duplicate
instance holding the same starting checkpoint, an attempt whose commit
never reaches the record. Coming through that swap is the job. After
it, the services must again agree on one append-only tree — the tree
the record names — and a process that starts cold on the services has
to be able to put them back in that agreement, because the drill does
not resubmit work that was in flight when a writer died. The log must
then keep growing: submissions made after the swap commit on the
recovered tree, and a client that holds a `committed` answer can
already resolve its entries against the published objects. A tree the
record names stays committed — an instance still racing it from an
older checkpoint does not get to rewrite that tree or move the record,
however its own attempt ends up — and an attempt that never reached
the record has committed nothing: whatever it left behind must not
decide what the log becomes. That holds for an instance that lost a
race, too: once the competing commits stop, the next submission sent
to that same process commits on the current tree without a restart.

## What must be preserved

* The deployed contract in `/app/README.md`: CLI and config, HTTP
  request/response shapes, the checkpoint record's item schema, the
  public key layout and tile/checkpoint byte formats, the logical
  clock, and the codec (`treekit.py`) as the shared definition of
  both.
* The control-channel pauses: a worker must keep announcing
  `startup_complete`, `batch_assembled` and `before_reply`, with the
  same names and facts, at the same points in its flow. The drills
  hold and release workers at these pauses.
* The checkpoint record remains the only coordination mechanism
  between workers, and it remains a checkpoint record: the DynamoDB
  table continues to hold checkpoint records only, and entry or tile
  bytes continue to live in the object bucket. No queues, leases,
  owner flags, TTLs or operator intervention are available in a drill
  (nobody deletes a lock or waits out a lease for you).
* Endpoints given in the config are used as given.
* Workers keep running as separate processes in separate directories:
  a file written by one worker's process is not visible to another.

## How to run it

The emulator is already running at `http://localhost:4566`
(`bash /usr/local/bin/start_floci.sh` if it is not). To drive the
worker by hand:

```sh
aws --endpoint-url=http://localhost:4566 dynamodb create-table \
    --table-name checkpoints \
    --key-schema AttributeName=logID,KeyType=HASH \
    --attribute-definitions AttributeName=logID,AttributeType=B \
    --billing-mode PAY_PER_REQUEST
aws --endpoint-url=http://localhost:4566 s3 mb s3://tilelog
# install the empty checkpoint (origin/size/root/time, see README.md)
# and the published copy, then:
python3 /app/sequencer.py --config /app/config.sample.json &
curl -s http://127.0.0.1:8080/health
curl -s -X POST http://127.0.0.1:8080/submit \
    -d '{"request_id":"r1","entries":["'$(head -c 24 /dev/urandom | base64)'"]}'
```

`python3 /app/sequencer.py --help` prints the program's own usage. The
program logs to stdout.

Deliver the fixed program in place under `/app` (the graders run the
workers from there).
