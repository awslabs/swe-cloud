# background-job-broker: binary protocol

All binary traffic uses one packet layout, in both directions:

```
4 bytes   magic: "\0REQ" (sent to the server) or "\0RES" (sent by the server)
4 bytes   packet type, unsigned 32-bit big-endian
4 bytes   payload length in bytes, unsigned 32-bit big-endian
N bytes   payload: the arguments, separated by single NUL bytes
```

The last argument is not NUL-terminated and may itself contain NUL bytes (job
workloads and results are binary-safe). Packets with no arguments have length 0.
A connection may carry any number of packets; responses are sent in order.
The first byte of a connection selects the protocol: NUL means binary packets,
anything else means text admin commands (see `interface.md`).

## Packet types in scope

| type | name | direction | arguments |
| ---: | --- | --- | --- |
| 1 | CAN_DO | worker → server | function name |
| 2 | CANT_DO | worker → server | function name |
| 3 | RESET_ABILITIES | worker → server | — |
| 4 | PRE_SLEEP | worker → server | — |
| 6 | NOOP | server → worker | — |
| 7 | SUBMIT_JOB | client → server | function, unique id, workload |
| 8 | JOB_CREATED | server → client | job handle |
| 9 | GRAB_JOB | worker → server | — |
| 10 | NO_JOB | server → worker | — |
| 11 | JOB_ASSIGN | server → worker | job handle, function, workload |
| 12 | WORK_STATUS | worker → server, server → client | job handle, numerator, denominator |
| 13 | WORK_COMPLETE | worker → server, server → client | job handle, result |
| 14 | WORK_FAIL | worker → server, server → client | job handle |
| 15 | GET_STATUS | client → server | job handle |
| 16 | ECHO_REQ | any → server | opaque data |
| 17 | ECHO_RES | server → sender | the same data |
| 18 | SUBMIT_JOB_BG | client → server | function, unique id, workload |
| 19 | ERROR | server → sender | error code, error text |
| 20 | STATUS_RES | server → client | job handle, known, running, numerator, denominator |
| 21 | SUBMIT_JOB_HIGH | client → server | function, unique id, workload |
| 22 | SET_CLIENT_ID | worker → server | client id |
| 30 | GRAB_JOB_UNIQ | worker → server | — |
| 31 | JOB_ASSIGN_UNIQ | server → worker | job handle, function, unique id, workload |
| 32 | SUBMIT_JOB_HIGH_BG | client → server | function, unique id, workload |
| 33 | SUBMIT_JOB_LOW | client → server | function, unique id, workload |
| 34 | SUBMIT_JOB_LOW_BG | client → server | function, unique id, workload |
| 36 | SUBMIT_JOB_EPOCH | client → server | function, unique id, epoch seconds (decimal), workload |

## Roles

- **Client.** A `SUBMIT_JOB*` packet is answered with `JOB_CREATED` carrying a job
  handle. `SUBMIT_JOB`, `SUBMIT_JOB_HIGH` and `SUBMIT_JOB_LOW` are foreground jobs:
  the submitting connection later receives the job's `WORK_STATUS`,
  `WORK_COMPLETE` or `WORK_FAIL` packets. The `_BG` variants and
  `SUBMIT_JOB_EPOCH` are background jobs: the client receives only the handle.
  The three priority levels are high, normal (no suffix) and low.
  `SUBMIT_JOB_EPOCH` asks for the job to run at the given Unix time.
  `GET_STATUS` reports, for a handle, whether the job is known, whether it is
  running and the last reported progress.
- **Worker.** A worker announces functions with `CAN_DO`, then asks for work with
  `GRAB_JOB` / `GRAB_JOB_UNIQ`. The server answers with an assignment or
  `NO_JOB`. A worker may send `PRE_SLEEP`; the server then sends `NOOP` when work
  may be available, after which the worker grabs again. A worker finishes an
  assignment with `WORK_COMPLETE` or `WORK_FAIL` (optionally preceded by
  `WORK_STATUS`). The worker's connection closing while it holds a job is also
  observable.
- **Unique ids.** The unique id argument may be empty. Both the empty id and the
  single byte `-` are special inputs whose effects on coalescing of submissions,
  on persisted rows and on delivered assignments are part of the behaviour to
  observe.

`ERROR` packets report a code and a message; the code string is the stable part.
