# On-call excerpts, September

Raw lines as kept: worker stdout (`event` lines), the supervisor's own log
(`src: supervisor`), the host agent (`src: host`, with the process id it
watches) and consistent reads of the task row (`src: table`). Fleet
configuration at the time: `VISIBILITY_S = 40`, supervisor sweeping the
queue and forwarding what it receives with `ADOPT`.

## t-7c1

```
{"event": "HELD", "worker_id": "B", "at": 1789301000.41, "task_id": "t-7c1", "message_id": "8b2f0d6e-51a4-4c1e-9d3b-2f6a1e70c4d9", "receipt": "4be1c09a7d21"}
{"src": "table", "at": 1789301001.02, "task_id": "t-7c1", "holder": "B", "phase": "PENDING", "effect_count": 0, "due_at": 1789301095.0}
{"src": "host", "at": 1789301009.80, "worker_id": "B", "pid": 12041, "what": "oom-kill"}
{"src": "supervisor", "at": 1789301040.62, "action": "ADOPT", "to": "S", "message_id": "8b2f0d6e-51a4-4c1e-9d3b-2f6a1e70c4d9", "receive_count": 2, "receipt": "a91c3e0f5b77"}
{"event": "HELD", "worker_id": "S", "at": 1789301040.70, "task_id": "t-7c1", "message_id": "8b2f0d6e-51a4-4c1e-9d3b-2f6a1e70c4d9", "receipt": "a91c3e0f5b77"}
{"src": "table", "at": 1789301041.03, "task_id": "t-7c1", "holder": "S", "phase": "PENDING", "effect_count": 0, "due_at": 1789301095.0}
{"event": "IDLE", "worker_id": "S", "at": 1789301055.12}
{"src": "supervisor", "at": 1789301066.15, "action": "ADOPT", "to": "A", "message_id": "8b2f0d6e-51a4-4c1e-9d3b-2f6a1e70c4d9", "receive_count": 3, "receipt": "0e6d2b94c3a8"}
{"event": "HELD", "worker_id": "A", "at": 1789301066.22, "task_id": "t-7c1", "message_id": "8b2f0d6e-51a4-4c1e-9d3b-2f6a1e70c4d9", "receipt": "0e6d2b94c3a8"}
{"src": "table", "at": 1789301067.01, "task_id": "t-7c1", "holder": "A", "phase": "PENDING", "effect_count": 0, "due_at": 1789301095.0}
{"event": "IDLE", "worker_id": "S", "at": 1789301070.31}
{"src": "host", "at": 1789301081.50, "worker_id": "A", "pid": 12066, "what": "frozen"}
{"event": "FIRED", "worker_id": "S", "at": 1789301095.06, "task_id": "t-7c1"}
{"src": "table", "at": 1789301096.00, "task_id": "t-7c1", "holder": "A", "phase": "DONE", "effect_count": 1, "done_at": 1789301095.04}
{"src": "host", "at": 1789301128.90, "worker_id": "A", "pid": 12066, "what": "thawed"}
{"event": "FIRED", "worker_id": "A", "at": 1789301128.95, "task_id": "t-7c1"}
{"src": "table", "at": 1789301129.50, "task_id": "t-7c1", "holder": "A", "phase": "DONE", "effect_count": 2, "done_at": 1789301128.93}
```

## t-19e

```
{"event": "HELD", "worker_id": "A", "at": 1789402000.10, "task_id": "t-19e", "message_id": "3d7ac215-0f8e-4b62-a1c9-58e04b9d6f13", "receipt": "c2f8a0614e9b"}
{"src": "table", "at": 1789402001.00, "task_id": "t-19e", "holder": "A", "phase": "PENDING", "effect_count": 0, "due_at": 1789402095.0}
{"src": "host", "at": 1789402011.00, "worker_id": "A", "pid": 8127, "what": "frozen"}
{"src": "supervisor", "at": 1789402040.31, "action": "ADOPT", "to": "S", "message_id": "3d7ac215-0f8e-4b62-a1c9-58e04b9d6f13", "receive_count": 2, "receipt": "71b9e4d0a2c6"}
{"event": "HELD", "worker_id": "S", "at": 1789402040.35, "task_id": "t-19e", "message_id": "3d7ac215-0f8e-4b62-a1c9-58e04b9d6f13", "receipt": "71b9e4d0a2c6"}
{"src": "table", "at": 1789402041.02, "task_id": "t-19e", "holder": "S", "phase": "PENDING", "effect_count": 0, "due_at": 1789402095.0}
{"src": "host", "at": 1789402056.00, "worker_id": "A", "pid": 8127, "what": "thawed"}
{"src": "host", "at": 1789402094.20, "worker_id": "S", "pid": 8133, "what": "frozen"}
{"event": "FIRED", "worker_id": "A", "at": 1789402095.03, "task_id": "t-19e"}
{"src": "table", "at": 1789402095.60, "task_id": "t-19e", "holder": "S", "phase": "DONE", "effect_count": 1, "done_at": 1789402095.01}
{"src": "host", "at": 1789402096.90, "worker_id": "S", "pid": 8133, "what": "thawed"}
{"event": "FIRED", "worker_id": "S", "at": 1789402096.95, "task_id": "t-19e"}
{"src": "table", "at": 1789402097.40, "task_id": "t-19e", "holder": "S", "phase": "DONE", "effect_count": 2, "done_at": 1789402096.93}
```

## t-33a

```
{"event": "HELD", "worker_id": "A", "at": 1789503000.10, "task_id": "t-33a", "message_id": "b64f9a21-7c0e-4d3a-9f57-2ad1c8e44b07", "receipt": "9d34c1aa07e5"}
{"src": "table", "at": 1789503000.95, "task_id": "t-33a", "holder": "A", "phase": "PENDING", "effect_count": 0, "due_at": 1789503095.0}
{"src": "host", "at": 1789503011.80, "worker_id": "A", "pid": 9614, "what": "frozen"}
{"src": "supervisor", "at": 1789503015.42, "action": "restart", "worker_id": "A", "old_pid": 9614, "new_pid": 9702}
{"event": "READY", "worker_id": "A", "at": 1789503016.91}
{"src": "supervisor", "at": 1789503040.55, "action": "ADOPT", "to": "A", "message_id": "b64f9a21-7c0e-4d3a-9f57-2ad1c8e44b07", "receive_count": 2, "receipt": "5cf2e8b9d4a1"}
{"event": "HELD", "worker_id": "A", "at": 1789503040.63, "task_id": "t-33a", "message_id": "b64f9a21-7c0e-4d3a-9f57-2ad1c8e44b07", "receipt": "5cf2e8b9d4a1"}
{"src": "table", "at": 1789503041.10, "task_id": "t-33a", "holder": "A", "phase": "PENDING", "effect_count": 0, "due_at": 1789503095.0}
{"src": "host", "at": 1789503044.00, "worker_id": "A", "pid": 9614, "what": "thawed"}
{"event": "IDLE", "worker_id": "A", "at": 1789503062.30}
{"src": "table", "at": 1789503070.30, "task_id": "t-33a", "holder": "A", "phase": "PENDING", "effect_count": 0, "due_at": 1789503095.0}
{"src": "host", "at": 1789503092.40, "worker_id": "A", "pid": 9702, "what": "frozen"}
{"event": "FIRED", "worker_id": "A", "at": 1789503095.12, "task_id": "t-33a"}
{"src": "table", "at": 1789503095.20, "task_id": "t-33a", "holder": "A", "phase": "DONE", "effect_count": 1, "done_at": 1789503095.08}
{"src": "host", "at": 1789503095.30, "worker_id": "A", "pid": 9702, "what": "thawed"}
{"event": "FIRED", "worker_id": "A", "at": 1789503095.44, "task_id": "t-33a"}
{"src": "table", "at": 1789503096.10, "task_id": "t-33a", "holder": "A", "phase": "DONE", "effect_count": 2, "done_at": 1789503095.41}
```
