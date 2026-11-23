"""Operator tooling.

    python -m orchestrator.admin deployment NAME --limit N [--strategy ENQUEUE|CANCEL_NEW]
    python -m orchestrator.admin run DEPLOYMENT [--retries N] [--retry-delay S] [--plan 1:fail,16:fail]
                                     [--then 3:complete] [--at +SECONDS]
    python -m orchestrator.admin runs                     every run: state, run_count, lease
    python -m orchestrator.admin history RUN_ID           a run's states and attempts
    python -m orchestrator.admin slots                    concurrency limits and active slots
    python -m orchestrator.admin leases                   lease records in Valkey
    python -m orchestrator.admin set-state RUN_ID TYPE [--name NAME] [--force]

`set-state ... CRASHED --force` is how a run whose node vanished is closed out: nobody else will
report it, so an operator (or the stale-run sweeper, which calls the same API) does, sometimes long
after its lease ran out. Talks to Postgres and Valkey directly (PG_DSN, VALKEY_URL) except
`set-state`, which goes through the orchestrator API (ORCH_URL).
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from . import config, db
from .leases import EXPIRATIONS_KEY, LEASE_PREFIX, LeaseStorage


def _plan(text: str | None) -> list[dict]:
    steps = []
    for item in filter(None, (text or "").split(",")):
        seconds, _, outcome = item.partition(":")
        steps.append({"seconds": float(seconds), "outcome": outcome or "complete"})
    return steps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("deployment")
    p.add_argument("name")
    p.add_argument("--limit", type=int, required=True)
    p.add_argument("--strategy", default="ENQUEUE")
    p = sub.add_parser("run")
    p.add_argument("deployment")
    p.add_argument("--retries", type=int, default=0)
    p.add_argument("--retry-delay", type=int, default=0)
    p.add_argument("--plan", default="")
    p.add_argument("--then", default="3:complete")
    p.add_argument("--at", default="+0")
    sub.add_parser("runs")
    p = sub.add_parser("history")
    p.add_argument("run_id")
    sub.add_parser("slots")
    sub.add_parser("leases")
    p = sub.add_parser("set-state")
    p.add_argument("run_id")
    p.add_argument("type")
    p.add_argument("--name")
    p.add_argument("--force", action="store_true")
    args = parser.parse_args()

    settings = config.load()
    if args.command == "set-state":
        body = json.dumps({"type": args.type, "name": args.name, "force": args.force}).encode()
        url = os.environ.get("ORCH_URL", f"http://127.0.0.1:{settings.port}").rstrip("/")
        req = urllib.request.Request(f"{url}/flow_runs/{args.run_id}/set_state", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(resp.read().decode())
        return
    if args.command == "leases":
        leases = LeaseStorage(settings.valkey_url)
        for member, score in leases.client.zrange(EXPIRATIONS_KEY, 0, -1, withscores=True):
            left = score - datetime.now(timezone.utc).timestamp()
            print(f"{member}  expires in {left:6.1f}s  {leases.client.get(LEASE_PREFIX + member)}")
        return

    conn = db.connect(settings.pg_dsn)
    db.ensure_schema(conn)
    if args.command == "deployment":
        limit_id, dep_id = uuid.uuid4(), uuid.uuid4()
        with conn.transaction():
            conn.execute('INSERT INTO concurrency_limit_v2 (id, name, "limit") VALUES (%s, %s, %s)',
                         (limit_id, f"deployment:{dep_id}", args.limit))
            conn.execute("INSERT INTO deployment (id, name, concurrency_limit_id, collision_strategy) "
                         "VALUES (%s, %s, %s, %s)", (dep_id, args.name, limit_id, args.strategy))
        print(dep_id)
    elif args.command == "run":
        dep = conn.execute("SELECT id FROM deployment WHERE name = %s OR id::text = %s",
                           (args.deployment, args.deployment)).fetchone()
        if dep is None:
            raise SystemExit(f"no deployment {args.deployment}")
        run_id = uuid.uuid4()
        at = datetime.now(timezone.utc) + timedelta(seconds=float(args.at.lstrip("+")))
        params = {"attempts": _plan(args.plan), "otherwise": (_plan(args.then) or [None])[0]}
        with conn.transaction():
            conn.execute(
                "INSERT INTO flow_run (id, deployment_id, parameters, retries, retry_delay_s, state_type, "
                "state_name, scheduled_time) VALUES (%s, %s, %s, %s, %s, 'SCHEDULED', 'Scheduled', %s)",
                (run_id, dep["id"], json.dumps(params), args.retries, args.retry_delay, at))
            conn.execute("INSERT INTO flow_run_state (flow_run_id, type, name, scheduled_time) "
                         "VALUES (%s, 'SCHEDULED', 'Scheduled', %s)", (run_id, at))
        print(run_id)
    elif args.command == "runs":
        for r in conn.execute(
            "SELECT r.id, d.name AS dep, r.state_type, r.state_name, r.run_count, r.retry_type, r.lease_id "
            "FROM flow_run r LEFT JOIN deployment d ON d.id = r.deployment_id ORDER BY r.created_seq"
        ).fetchall():
            print(f"{r['id']}  {r['dep']:<16} {r['state_type']:<10} {r['state_name']:<24} "
                  f"run_count={r['run_count']} retry_type={r['retry_type']} lease={r['lease_id']}")
    elif args.command == "history":
        for s in conn.execute("SELECT ts, type, name, lease_id, scheduled_time FROM flow_run_state "
                              "WHERE flow_run_id = %s ORDER BY id", (args.run_id,)).fetchall():
            print(f"{s['ts']:%H:%M:%S.%f}  {s['type']:<10} {s['name']:<24} lease={s['lease_id']} "
                  f"scheduled={s['scheduled_time']}")
        for a in conn.execute("SELECT attempt, lease_id, body_start, body_end, outcome FROM attempt_log "
                              "WHERE flow_run_id = %s ORDER BY id", (args.run_id,)).fetchall():
            print(f"attempt {a['attempt']}: {a['body_start']:%H:%M:%S.%f} -> "
                  f"{a['body_end'] and format(a['body_end'], '%H:%M:%S.%f')}  {a['outcome']} lease={a['lease_id']}")
    elif args.command == "slots":
        for c in conn.execute('SELECT c.id, c."limit", c.active_slots, d.name FROM concurrency_limit_v2 c '
                              "LEFT JOIN deployment d ON d.concurrency_limit_id = c.id ORDER BY d.name").fetchall():
            print(f"{c['name']:<16} limit={c['limit']} active_slots={c['active_slots']}  ({c['id']})")


if __name__ == "__main__":
    main()
