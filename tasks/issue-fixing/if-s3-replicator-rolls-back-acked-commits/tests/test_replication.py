"""Verifier: drives `node serve` / `node restore` through a fault proxy and grades the bucket.

Runs (each REPEATS times, fresh bucket and database name):
  C   control: checkpoint with no faults, clean shutdown, restart on the same disk.
  K0  crash while the new generation's snapshot upload is in flight, nothing committed after
      rollover; then boot a disk copied at seq 5.
  K5  same, with a failed first .dep PUT and five commits acknowledged into the new generation
      before the crash; then boot a disk copied at seq 10.
  T   store throttling while the new generation's snapshot is in flight: a burst of 503 SlowDown
      on every PUT, whose first frame PUT the store completes but the gateway answers 504; then
      commits, a checkpoint and a crash once the burst has passed.
  L   a bucket the deployed writer left behind, whose newest generation is a restart
      generation without .dep (snapshot = its predecessor's last state): restore it, boot an
      empty disk on it with snapshots held, commit, crash, restore.
  R   the remnant of incident 2: the newest generation, without .dep, was published from an
      old volume and forks the history; restore must not report success with a lost commit.
"""
import json
import os
import re
import time
import traceback
import uuid
from pathlib import Path

import boto3
from cdk_seed.cfn_deploy import deploy

import pytest

import harness as H
from faultproxy import FaultProxy

REPEATS = int(os.environ.get("STRATA_REPEATS", "3"))
LOGROOT = "/logs/verifier/strata" if os.path.isdir("/logs/verifier") else os.path.join(H.ROOT, "logs")
os.makedirs(LOGROOT, exist_ok=True)

BASE = 10
HOLD_EXCLUDE = r"/(\.dep|\d{12}-\d{12}-\d+\.jsonl)$"

CONTROL_CHECKS = ["commits", "snapshot", "shutdown", "restore", "restart_restore", "chain",
                  "hygiene"]
CRASH_CHECKS = ["commits", "probe", "checkpoint_prompt", "hold_engaged", "restore_after_crash",
                "stale_boot", "stale_restore", "stale_chain", "hygiene"]
K5_CHECKS = CRASH_CHECKS + ["acks_prompt", "layout_after_crash"]
THROTTLE_CHECKS = ["commits", "checkpoint_prompt", "hold_engaged", "acks_prompt", "burst_acks",
                   "checkpoint_after_burst", "acks_after_burst", "restore_after_burst",
                   "history", "hygiene"]

BURST_S = 5.0
BURST_SETTLE_S = 0.5
SNAPSHOT_AFTER_BURST_WAIT_S = 15.0

LEGACY_SEQ = 10
LEGACY_CHECKS = ["legacy_restore", "legacy_boot", "legacy_commits", "legacy_restore_after_crash",
                 "legacy_chain", "hygiene"]
ROLLBACK_CHECKS = ["rollback_restore", "hygiene"]


class Run:
    def __init__(self, kind, idx, checks):
        self.kind, self.idx, self.expected = kind, idx, list(checks)
        self.results, self.notes = {}, []

    def check(self, name, ok, detail=""):
        if name in self.results and not self.results[name][0]:
            return
        self.results[name] = (bool(ok), detail if not ok else "")

    def note(self, text):
        self.notes.append(text)

    def abort(self, reason):
        for c in self.expected:
            if c not in self.results:
                self.results[c] = (False, f"not reached: {reason}")


class Ctx:
    def __init__(self, run):
        self.s3 = H.direct_client()
        self.bucket = f"strata-{uuid.uuid4().hex[:12]}"
        self.name = f"db{uuid.uuid4().hex[:8]}"
        cfn = boto3.client("cloudformation", endpoint_url=self.s3.meta.endpoint_url,
                           region_name=self.s3.meta.region_name)
        deploy(cfn, "replication-seed-%s" % self.bucket,
               Path(__file__).parent / "cdk_seed/seed.template.json",
               BucketName=self.bucket)
        self.proxy = FaultProxy(H.FLOCI_PORT, H.free_port()).start()
        self.endpoint = f"http://127.0.0.1:{self.proxy.port}"
        self.logdir = os.path.join(LOGROOT, f"{run.kind}-{run.idx}-{self.name}")
        os.makedirs(self.logdir, exist_ok=True)
        self.listings = []
        self.quiet_restores = []   # restore windows with no node alive
        self.nodes = []

    def node(self, db_dir):
        n = H.Node(db_dir, self.bucket, self.name, self.endpoint, self.logdir)
        self.nodes.append(n)
        return n.start()

    def snap(self):
        lst = H.listing(self.s3, self.bucket)
        self.listings.append(lst)
        return lst

    def restore(self, quiet):
        r = H.run_restore(self.bucket, self.name, self.endpoint, self.logdir)
        if quiet:
            self.quiet_restores.append((r["t0"], r["t1"]))
        return r

    def close(self):
        for n in self.nodes:
            try:
                n.kill()
            except Exception:
                pass
        self.proxy.stop()


def execute(fn, kind, idx, checks):
    run = Run(kind, idx, checks)
    ctx = None
    try:
        ctx = Ctx(run)
        fn(run, ctx)
    except Exception as e:
        run.note(traceback.format_exc()[-2000:])
        run.abort(f"{type(e).__name__}: {e}")
    finally:
        if ctx is not None:
            try:
                with open(os.path.join(ctx.logdir, "proxy-log.json"), "w") as f:
                    json.dump(ctx.proxy.get_log(), f, indent=0)
            except Exception:
                pass
            ctx.close()
    run.abort("scenario ended early")
    with open(os.path.join(LOGROOT, f"{kind}-{idx}.json"), "w") as f:
        json.dump({"results": run.results, "notes": run.notes}, f, indent=1)
    return run


# ------------------------------------------------------------------------ shared checks

def commit_range(node, ids):
    bad = []
    for i in ids:
        st, obj, lat = node.commit(i)
        if st != 200:
            bad.append(f"commit {i}: status {st} {obj}")
    return bad


def restore_problems(ctx, r, n):
    """A restore after n acknowledged commits must give ids 1..n, head n, seq n, and agree with
    the bucket read by the layout contract."""
    want = list(range(1, n + 1))
    if r["rc"] != 0 or not r["out"]:
        return [f"restore exit {r['rc']}, output {r['out']}; stderr: {r['stderr_tail']}"]
    out = r["out"]
    problems = []
    if out.get("ids") != want:
        missing = sorted(set(want) - set(out.get("ids") or []))
        extra = sorted(set(out.get("ids") or []) - set(want))
        problems.append(f"restore ids missing {missing} extra {extra}")
    if out.get("head") != n:
        problems.append(f"restore head {out.get('head')} != {n}")
    if out.get("seq") != n:
        problems.append(f"restore seq {out.get('seq')} != {n}")
    ref = H.reference_restore(ctx.s3, ctx.bucket, ctx.name)
    if ref.get("refuse"):
        problems.append("bucket chain exceeds the restore depth")
    else:
        if ref["ids"] != want or ref["head"] != n:
            missing = sorted(set(want) - set(ref["ids"]))
            problems.append(f"bucket chain from the newest generation {ref['generation']} holds "
                            f"head {ref['head']}, missing ids {missing}")
        for k in ("seq", "head", "ids"):
            if out.get(k) != ref[k]:
                problems.append(f"restore {k} differs from the bucket's chain state")
    return problems


def chain_problems(ctx, g1, floor, before):
    """Every generation newer than G1 must name the next-listed generation in .dep; any new
    generation's snapshot counter must be at least the state the bucket already held."""
    order, objs = H.generations(H.listing(ctx.s3, ctx.bucket).keys(), ctx.name)
    if g1 not in order:
        return [f"first generation {g1} no longer listed"]
    problems = []
    idx = order.index(g1)
    for i in range(idx):
        g, nxt = order[i], order[i + 1]
        if ".dep" not in objs[g]:
            problems.append(f"generation {g} has no .dep")
            continue
        dep = H.get_bytes(ctx.s3, ctx.bucket, f"{ctx.name}-{g}/.dep").decode(errors="replace").strip()
        if dep != nxt:
            problems.append(f"generation {g} .dep names {dep}, next listed is {nxt}")
    if floor is not None:
        old = set(H.generations(before.keys(), ctx.name)[0])
        for g in order:
            if g in old or ".changecounter" not in objs[g]:
                continue
            raw = H.get_bytes(ctx.s3, ctx.bucket, f"{ctx.name}-{g}/.changecounter")
            try:
                val = int(raw.decode().strip())
            except ValueError:
                continue
            if val < floor:
                problems.append(f"generation {g} was published with .changecounter {val}, "
                                f"older than the {floor} the bucket already held")
    return problems


def hygiene_problems(ctx):
    final = ctx.snap()
    allow = H.allowlist(ctx.name)
    problems = []
    for k in final:
        if not allow.match(k):
            problems.append(f"unexpected object {k}")
    for earlier in ctx.listings[:-1]:
        for k, (etag, _) in earlier.items():
            if k not in final:
                problems.append(f"object {k} disappeared")
            elif final[k][0] != etag:
                problems.append(f"object {k} was overwritten")
    stored = {}
    for e in ctx.proxy.get_log():
        if e["method"] == "PUT" and e["key"] and e["upstream"] and 200 <= e["upstream"] < 300:
            stored.setdefault(e["key"], set()).add(e["sha"])
    for k, shas in stored.items():
        if len(shas) > 1:
            problems.append(f"{k} was stored with {len(shas)} different contents")
    for start, end in ctx.quiet_restores:
        for e in ctx.proxy.get_log():
            if start <= e["ts"] <= end and e["method"] in H.WRITE_METHODS:
                problems.append(f"node restore sent {e['method']} {e['key']}")
    order, objs = H.generations(final.keys(), ctx.name)
    for g in order:
        pre = f"{ctx.name}-{g}/"
        for o in objs[g]:
            if o != "db.sqlite" and final[pre + o][1] > H.SMALL_OBJECT_MAX_BYTES:
                problems.append(f"{pre + o} is {final[pre + o][1]} bytes")
        cc = None
        if ".dep" in objs[g]:
            dep = H.get_bytes(ctx.s3, ctx.bucket, pre + ".dep").decode(errors="replace").strip()
            if not re.fullmatch(H.GEN_RE, dep):
                problems.append(f"{pre}.dep is not a generation id")
        if ".changecounter" in objs[g]:
            raw = H.get_bytes(ctx.s3, ctx.bucket, pre + ".changecounter").decode(errors="replace")
            try:
                cc = int(raw.strip())
            except ValueError:
                problems.append(f"{pre}.changecounter is not an integer")
        if "db.sqlite" in objs[g]:
            try:
                seq, _, _ = H.read_snapshot(ctx.s3, ctx.bucket, pre + "db.sqlite")
                if cc is not None and seq != cc:
                    problems.append(f"{pre}db.sqlite seq {seq} != .changecounter {cc}")
            except Exception as e:
                problems.append(f"{pre}db.sqlite unreadable: {e}")
    return problems


def report(run, name, problems):
    run.check(name, not problems, "; ".join(problems[:6]))


# ------------------------------------------------------------------------ scenarios

def scenario_control(run, ctx):
    dir_a = H.private_dir("node")
    node = ctx.node(dir_a)
    g1 = node.wait_healthy()["generation"]
    report(run, "commits", commit_range(node, range(1, BASE + 1)))
    ctx.snap()
    st, cp, lat = node.checkpoint()
    g2 = cp.get("new") if st == 200 else None
    problems = [] if st == 200 and g2 and g2 != g1 else [f"checkpoint returned {st} {cp}"]

    def complete():
        objs = H.generations(H.listing(ctx.s3, ctx.bucket).keys(), ctx.name)[1].get(g2, [])
        return {".dep", "db.sqlite", ".changecounter"} <= set(objs)

    if not problems:
        done, _ = H.wait_for(complete, H.SNAPSHOT_COMPLETE_DEADLINE_S)
        if not done:
            problems.append(f"new generation {g2} lacks .dep/db.sqlite/.changecounter "
                            f"{H.SNAPSHOT_COMPLETE_DEADLINE_S}s after checkpoint")
        else:
            pre = f"{ctx.name}-{g2}/"
            dep = H.get_bytes(ctx.s3, ctx.bucket, pre + ".dep").decode(errors="replace").strip()
            cc = H.get_bytes(ctx.s3, ctx.bucket, pre + ".changecounter").decode(errors="replace")
            if dep != g1:
                problems.append(f".dep of {g2} is {dep}, expected {g1}")
            if cc.strip() != str(BASE):
                problems.append(f".changecounter of {g2} is {cc!r}, expected {BASE}")
    report(run, "snapshot", problems)
    report(run, "commits", commit_range(node, [BASE + 1]))
    rc = node.sigterm()
    report(run, "shutdown", [] if rc == 0 else [f"SIGTERM exit {rc}"])
    ctx.snap()
    report(run, "restore", restore_problems(ctx, ctx.restore(quiet=True), BASE + 1))

    node = ctx.node(dir_a)
    node.wait_healthy()
    problems = commit_range(node, [BASE + 2])
    rc = node.sigterm()
    if rc != 0:
        problems.append(f"SIGTERM exit {rc} after restart")
    problems += restore_problems(ctx, ctx.restore(quiet=True), BASE + 2)
    report(run, "restart_restore", problems)
    report(run, "chain", chain_problems(ctx, g1, None, {}))
    report(run, "hygiene", hygiene_problems(ctx))


def scenario_crash(run, ctx, post, stale_at, fail_dep):
    dir_a = H.private_dir("node")
    node = ctx.node(dir_a)
    g1 = node.wait_healthy()["generation"]
    problems = commit_range(node, range(1, stale_at + 1))

    probe = ctx.restore(quiet=False)
    stale_dir = probe["dir"]
    pp = []
    if probe["rc"] != 0 or not probe["out"] or probe["out"].get("seq") != stale_at \
            or probe["out"].get("ids") != list(range(1, stale_at + 1)):
        pp.append(f"restore beside the live node at seq {stale_at}: rc {probe['rc']} "
                  f"output {probe['out']}")
    report(run, "probe", pp)
    stale_dir = H.hide(stale_dir)

    problems += commit_range(node, range(stale_at + 1, BASE + 1))
    report(run, "commits", problems)
    ctx.snap()

    ctx.proxy.arm_hold(rf"^{re.escape(ctx.name)}-(?!{g1})[^/]+/", HOLD_EXCLUDE)
    if fail_dep:
        ctx.proxy.arm_fail(rf"^{re.escape(ctx.name)}-(?!{g1})[^/]+/\.dep$", 1)
    st, cp, lat = node.checkpoint()
    g2 = cp.get("new") if st == 200 else None
    cpp = []
    if st != 200 or not g2 or g2 == g1:
        cpp.append(f"checkpoint returned {st} {cp} after {lat:.2f}s")
    elif lat >= H.CHECKPOINT_RETURN_DEADLINE_S:
        cpp.append(f"checkpoint took {lat:.2f}s while the snapshot upload was in flight")
    report(run, "checkpoint_prompt", cpp)

    def held_new():
        return [h for h in ctx.proxy.held_keys()
                if not h["key"].startswith(f"{ctx.name}-{g1}/")]

    held, _ = H.wait_for(held_new, H.HOLD_ENGAGE_DEADLINE_S)
    report(run, "hold_engaged", [] if held else [
        f"no snapshot upload under a new generation started within {H.HOLD_ENGAGE_DEADLINE_S}s "
        f"of the checkpoint"])

    if g2:
        dep_key = f"{ctx.name}-{g2}/.dep"
        seen, waited = H.wait_for(lambda: dep_key in H.listing(ctx.s3, ctx.bucket),
                                  H.DEP_VISIBLE_WAIT_S, interval=0.2)
        run.note(f".dep of {g2} listed={bool(seen)} after {waited:.1f}s")

    acks = []
    for i in range(BASE + 1, BASE + post + 1):
        acks.append((i,) + node.commit(i))
    still_held = bool(held_new())
    if post:
        ap = [f"commit {i}: status {s} after {lat:.2f}s" for i, s, _, lat in acks
              if s != 200 or lat >= H.COMMIT_ACK_DEADLINE_S]
        if not still_held:
            ap.append("the new generation's snapshot upload was no longer in flight at the last ack")
        report(run, "acks_prompt", ap)

    node.kill()
    H.hide(dir_a)
    after_kill = ctx.snap()

    if post:
        report(run, "layout_after_crash", layout_problems(ctx, after_kill, g1, g2, post))

    report(run, "restore_after_crash",
           restore_problems(ctx, ctx.restore(quiet=True), BASE + post))

    ctx.proxy.clear_rules()
    last = BASE + post + 1
    node = ctx.node(stale_dir)
    sb = []
    try:
        node.wait_healthy()
        sb += commit_range(node, [last])
        rc = node.sigterm()
        if rc != 0:
            sb.append(f"SIGTERM exit {rc}")
    except AssertionError as e:
        sb.append(str(e))
        node.kill()
    report(run, "stale_boot", sb)
    report(run, "stale_restore", restore_problems(ctx, ctx.restore(quiet=True), last))
    report(run, "stale_chain", chain_problems(ctx, g1, BASE + post, after_kill))
    report(run, "hygiene", hygiene_problems(ctx))


def layout_problems(ctx, lst, g1, g2, post):
    order, objs = H.generations(lst.keys(), ctx.name)
    if not g2:
        return ["no new generation"]
    if not order or order[0] != g2:
        return [f"newest listed generation is {order[0] if order else None}, not {g2}"]
    problems = []
    names = objs[g2]
    if ".dep" not in names:
        problems.append(f"{g2} has no .dep")
    else:
        dep = H.get_bytes(ctx.s3, ctx.bucket, f"{ctx.name}-{g2}/.dep").decode(errors="replace").strip()
        if dep != g1:
            problems.append(f"{g2} .dep names {dep}, not {g1}")
    for o in ("db.sqlite", ".changecounter"):
        if o in names:
            problems.append(f"{g2} holds {o} although its snapshot upload never completed")
    expected, seqs = 1, []
    for first, last, o in H.frame_batches(names):
        if first != expected:
            problems.append(f"{g2} frames are not contiguous from 1 (found {first} after "
                            f"{expected - 1})")
            break
        seqs += [int(r["seq"]) for r in H.read_records(ctx.s3, ctx.bucket, f"{ctx.name}-{g2}/{o}")]
        expected = last + 1
    want = list(range(BASE + 1, BASE + post + 1))
    if sorted(seqs) != want:
        problems.append(f"frames under {g2} carry seq {sorted(seqs)}, expected {want}")
    for _, _, o in H.frame_batches(objs.get(g1, [])):
        for r in H.read_records(ctx.s3, ctx.bucket, f"{ctx.name}-{g1}/{o}"):
            if int(r["seq"]) > BASE:
                problems.append(f"{g1} holds seq {r['seq']} after the rollover")
    return problems


def restore_set_problems(ctx, r, acked, attempted, last):
    """A restore must hold every acknowledged commit, nothing that was never sent, end on the
    last commit, count its commits in seq, and agree with the bucket read by the layout
    contract. Unacknowledged commits may be there or not."""
    if r["rc"] != 0 or not r["out"]:
        return [f"restore exit {r['rc']}, output {r['out']}; stderr: {r['stderr_tail']}"]
    out = r["out"]
    ids = set(out.get("ids") or [])
    problems = []
    missing = sorted(set(acked) - ids)
    if missing:
        problems.append(f"restore lost acknowledged ids {missing}")
    extra = sorted(ids - set(attempted))
    if extra:
        problems.append(f"restore holds ids {extra} that were never committed")
    if last is not None and out.get("head") != last:
        problems.append(f"restore head {out.get('head')} != {last}")
    if out.get("seq") != len(ids):
        problems.append(f"restore seq {out.get('seq')} but {len(ids)} commits")
    ref = H.reference_restore(ctx.s3, ctx.bucket, ctx.name)
    if ref.get("refuse"):
        problems.append("bucket chain exceeds the restore depth")
    else:
        lost = sorted(set(acked) - set(ref["ids"]))
        if lost:
            problems.append(f"bucket chain from the newest generation {ref['generation']} "
                            f"lacks acknowledged ids {lost}")
        for k in ("seq", "head", "ids"):
            if out.get(k) != ref[k]:
                problems.append(f"restore {k} differs from the bucket's chain state")
    return problems


def scenario_throttle(run, ctx):
    dir_a = H.private_dir("node")
    node = ctx.node(dir_a)
    g1 = node.wait_healthy()["generation"]
    report(run, "commits", commit_range(node, range(1, BASE + 1)))
    ctx.snap()

    ctx.proxy.arm_hold(rf"^{re.escape(ctx.name)}-(?!{g1})[^/]+/", HOLD_EXCLUDE)
    st, cp, lat = node.checkpoint()
    g2 = cp.get("new") if st == 200 else None
    cpp = []
    if st != 200 or not g2 or g2 == g1:
        cpp.append(f"checkpoint returned {st} {cp} after {lat:.2f}s")
    elif lat >= H.CHECKPOINT_RETURN_DEADLINE_S:
        cpp.append(f"checkpoint took {lat:.2f}s while the snapshot upload was in flight")
    report(run, "checkpoint_prompt", cpp)
    if not g2:
        raise AssertionError("no new generation")

    def held_under(excluded):
        return [h for h in ctx.proxy.held_keys()
                if not any(h["key"].startswith(f"{ctx.name}-{g}/") for g in excluded)]

    held, _ = H.wait_for(lambda: held_under([g1]), H.HOLD_ENGAGE_DEADLINE_S)
    report(run, "hold_engaged", [] if held else [
        f"no snapshot upload under a new generation started within {H.HOLD_ENGAGE_DEADLINE_S}s "
        f"of the checkpoint"])
    dep_key = f"{ctx.name}-{g2}/.dep"
    seen, waited = H.wait_for(lambda: dep_key in H.listing(ctx.s3, ctx.bucket),
                              H.DEP_VISIBLE_WAIT_S, interval=0.2)
    run.note(f".dep of {g2} listed={bool(seen)} after {waited:.1f}s")

    acked = set(range(1, BASE + 1))
    ap = []
    for i in (BASE + 1, BASE + 2):
        st, obj, lat = node.commit(i)
        if st != 200 or lat >= H.COMMIT_ACK_DEADLINE_S:
            ap.append(f"commit {i}: status {st} after {lat:.2f}s")
        elif st == 200:
            acked.add(i)
    report(run, "acks_prompt", ap)

    # The burst: every PUT is refused with 503 for BURST_S, except the first frame batch PUT,
    # which the store takes and the gateway answers 504. The held snapshot upload is refused too.
    # The commits sent meanwhile are answered (if within the ack limit) before it ends, so the
    # first thing the node is asked to do afterwards is a checkpoint.
    ctx.snap()
    t_burst = time.monotonic()
    ctx.proxy.arm_burst(BURST_S, rf"^{re.escape(ctx.name)}-[^/]+/\d{{12}}-\d{{12}}-\d+\.jsonl$")
    ctx.proxy.release_holds()
    attempted = set(acked)
    bp = []
    for i in (BASE + 3, BASE + 4):
        # only commits that end inside the burst when answered within the ack limit
        if i > BASE + 3 and time.monotonic() - t_burst > BURST_S - 2.5:
            break
        attempted.add(i)
        st, obj, lat = node.commit(i, timeout=BURST_S + 5.0)
        run.note(f"commit {i} during the burst: status {st} after {lat:.2f}s")
        if st == 200:
            if lat >= H.COMMIT_ACK_DEADLINE_S:
                bp.append(f"commit {i} acknowledged after {lat:.2f}s")
            else:
                acked.add(i)
    info = ctx.proxy.burst_info() or {}
    run.note(f"frame PUTs answered 504 after the store took them: {info.get('lost')}")
    remaining = BURST_S + BURST_SETTLE_S - (time.monotonic() - t_burst)
    if remaining > 0:
        time.sleep(remaining)
    report(run, "burst_acks", bp)

    def complete(g):
        objs = H.generations(H.listing(ctx.s3, ctx.bucket).keys(), ctx.name)[1].get(g, [])
        return {"db.sqlite", ".changecounter"} <= set(objs)

    done, waited = H.wait_for(lambda: complete(g2), SNAPSHOT_AFTER_BURST_WAIT_S, interval=0.2)
    run.note(f"snapshot of {g2} complete={bool(done)} {waited:.1f}s after the burst")

    ctx.proxy.arm_hold(rf"^{re.escape(ctx.name)}-(?!{g1}|{g2})[^/]+/", HOLD_EXCLUDE)
    st, cp, lat = node.checkpoint()
    g3 = cp.get("new") if st == 200 else None
    cb = []
    if st != 200 or not g3 or g3 in (g1, g2):
        cb.append(f"checkpoint after the burst returned {st} {cp} after {lat:.2f}s")
    elif lat >= H.CHECKPOINT_RETURN_DEADLINE_S:
        cb.append(f"checkpoint after the burst took {lat:.2f}s")
    else:
        held, _ = H.wait_for(lambda: held_under([g1, g2]), H.HOLD_ENGAGE_DEADLINE_S)
        if not held:
            cb.append(f"no snapshot upload under {g3} started within "
                      f"{H.HOLD_ENGAGE_DEADLINE_S}s of the checkpoint")
    report(run, "checkpoint_after_burst", cb)

    pa = []
    post = list(range(BASE + 6, BASE + 11))
    for i in post:
        attempted.add(i)
        st, obj, lat = node.commit(i)
        if st != 200 or lat >= H.COMMIT_ACK_DEADLINE_S:
            pa.append(f"commit {i} after the burst: status {st} {obj} after {lat:.2f}s")
        else:
            acked.add(i)
    report(run, "acks_after_burst", pa)

    node.kill()
    H.hide(dir_a)
    ctx.snap()
    last = post[-1] if post[-1] in acked else None
    report(run, "restore_after_burst",
           restore_set_problems(ctx, ctx.restore(quiet=True), acked, attempted, last))
    ctx.proxy.clear_rules()
    report(run, "history", H.history_problems(ctx.s3, ctx.bucket, ctx.name))
    report(run, "hygiene", hygiene_problems(ctx))


def seed_history(ctx):
    """Ids of three generations the deployed (pre-fix) writer created days ago, oldest first:
    GA the database's first generation (frames from seq 1, no snapshot), GB (.dep GA, snapshot at
    seq 4, then frames) and the newest, GC, made by the old boot path: a node start publishes a
    snapshot of its disk into a new generation and loses the .dep (observation 3)."""
    t0 = time.time() - 3 * 86400
    ga, gb, gc = (H.generation_at(t0 + k * 3600) for k in range(3))
    return ga, gb, gc, t0


def scenario_legacy(run, ctx):
    """A restart generation: GC's snapshot is GB's last state (seq 7), then frames seq 8..10."""
    ga, gb, gc, t0 = seed_history(ctx)
    recs = H.history_records([(s, s) for s in range(1, LEGACY_SEQ + 1)])
    H.seed_generation(ctx.s3, ctx.bucket, ctx.name, ga, t0, records=recs[0:4])
    H.seed_generation(ctx.s3, ctx.bucket, ctx.name, gb, t0 + 3600, dep=ga, base=recs[0:4],
                      records=recs[4:7])
    H.seed_generation(ctx.s3, ctx.bucket, ctx.name, gc, t0 + 7200, base=recs[0:7],
                      records=recs[7:LEGACY_SEQ])
    ctx.snap()
    report(run, "legacy_restore", restore_problems(ctx, ctx.restore(quiet=True), LEGACY_SEQ))

    # snapshots of every generation the node creates stay in flight
    ctx.proxy.arm_hold(rf"^{re.escape(ctx.name)}-(?!(?:{ga}|{gb}|{gc})/)[^/]+/", HOLD_EXCLUDE)
    dir_a = H.private_dir("node")
    node = ctx.node(dir_a)
    bp = []
    try:
        health = node.wait_healthy()
        if health.get("seq") != LEGACY_SEQ:
            bp.append(f"node booted on an empty disk reports seq {health.get('seq')}, the bucket "
                      f"holds {LEGACY_SEQ}")
    except AssertionError as e:
        bp.append(str(e))
    report(run, "legacy_boot", bp)
    if bp:
        node.kill()
        raise AssertionError("node did not boot on the deployed bucket")
    n = LEGACY_SEQ + 3
    ap = []
    for i in range(LEGACY_SEQ + 1, n + 1):
        st, obj, lat = node.commit(i)
        if st != 200 or lat >= H.COMMIT_ACK_DEADLINE_S:
            ap.append(f"commit {i}: status {st} {obj} after {lat:.2f}s")
    report(run, "legacy_commits", ap)
    node.kill()
    H.hide(dir_a)
    ctx.snap()
    report(run, "legacy_restore_after_crash",
           restore_problems(ctx, ctx.restore(quiet=True), n))
    ctx.proxy.clear_rules()
    report(run, "legacy_chain", chain_problems(ctx, gc, None, {}))
    report(run, "hygiene", hygiene_problems(ctx))


def scenario_rollback(run, ctx, restarted=False):
    """What incident 2 left behind: GB holds acknowledged commits 5..8; GC (no .dep) was
    published by a node on an old volume at seq 5 and took two acknowledged commits of its own.

    `restarted`: that node's snapshot never got stored (observation 4), it took three commits and
    was restarted on the same volume, so the newest generation GD (no .dep) is a complete
    snapshot of the old volume's history at seq 8, the same seq GB ends with."""
    ga, gb, gc, t0 = seed_history(ctx)
    main = H.history_records([(s, s) for s in range(1, 9)])
    H.seed_generation(ctx.s3, ctx.bucket, ctx.name, ga, t0, records=main[0:4])
    H.seed_generation(ctx.s3, ctx.bucket, ctx.name, gb, t0 + 3600, dep=ga, base=main[0:4],
                      records=main[4:8])
    if restarted:
        fork = H.history_records([(6, 101), (7, 102), (8, 103)])
        H.seed_generation(ctx.s3, ctx.bucket, ctx.name, gc, t0 + 7200, records=fork)
        H.seed_generation(ctx.s3, ctx.bucket, ctx.name, H.generation_at(t0 + 3 * 3600),
                          t0 + 3 * 3600, base=main[0:5] + fork)
    else:
        fork = H.history_records([(6, 101), (7, 102)])
        H.seed_generation(ctx.s3, ctx.bucket, ctx.name, gc, t0 + 7200, base=main[0:5],
                          records=fork)
    held = set(range(1, 9)) | {r["id"] for r in fork}
    ctx.snap()
    r = ctx.restore(quiet=True)
    problems = []
    if r["rc"] == 0 and r["out"]:
        missing = sorted(held - set(r["out"].get("ids") or []))
        if missing:
            problems.append(f"restore reported success with {r['out']}, without acknowledged ids "
                            f"{missing} the bucket holds")
    elif r["rc"] != H.RESTORE_REFUSE_EXIT_CODE:
        problems.append(f"restore exit {r['rc']}, output {r['out']}; stderr: {r['stderr_tail']}")
    report(run, "rollback_restore", problems)
    report(run, "hygiene", hygiene_problems(ctx))


# ------------------------------------------------------------------------ fixtures and tests

@pytest.fixture(scope="module")
def control_runs():
    return [execute(scenario_control, "C", i, CONTROL_CHECKS) for i in range(REPEATS)]


@pytest.fixture(scope="module")
def k0_runs():
    return [execute(lambda r, c: scenario_crash(r, c, post=0, stale_at=5, fail_dep=False),
                    "K0", i, CRASH_CHECKS) for i in range(REPEATS)]


@pytest.fixture(scope="module")
def k5_runs():
    return [execute(lambda r, c: scenario_crash(r, c, post=5, stale_at=10, fail_dep=True),
                    "K5", i, K5_CHECKS) for i in range(REPEATS)]


@pytest.fixture(scope="module")
def throttle_runs():
    return [execute(scenario_throttle, "T", i, THROTTLE_CHECKS) for i in range(REPEATS)]


def assert_check(runs, name):
    failures = []
    for r in runs:
        ok, detail = r.results.get(name, (False, "not recorded"))
        if not ok:
            failures.append(f"{r.kind}#{r.idx}: {detail}")
    assert not failures, "\n".join(failures)


def test_control_commits(control_runs):
    assert_check(control_runs, "commits")


def test_control_checkpoint_snapshot(control_runs):
    assert_check(control_runs, "snapshot")


def test_control_clean_shutdown(control_runs):
    assert_check(control_runs, "shutdown")


def test_control_restore(control_runs):
    assert_check(control_runs, "restore")


def test_control_restart_on_current_disk(control_runs):
    assert_check(control_runs, "restart_restore")


def test_control_generation_links(control_runs):
    assert_check(control_runs, "chain")


def test_control_bucket_layout(control_runs):
    assert_check(control_runs, "hygiene")


def test_k0_commits_and_probe(k0_runs):
    assert_check(k0_runs, "commits")
    assert_check(k0_runs, "probe")


def test_k0_checkpoint_does_not_wait(k0_runs):
    assert_check(k0_runs, "checkpoint_prompt")
    assert_check(k0_runs, "hold_engaged")


def test_k0_restore_after_crash(k0_runs):
    assert_check(k0_runs, "restore_after_crash")


def test_k0_old_disk_boot(k0_runs):
    assert_check(k0_runs, "stale_boot")
    assert_check(k0_runs, "stale_restore")


def test_k0_old_disk_publishes_nothing_older(k0_runs):
    assert_check(k0_runs, "stale_chain")


def test_k0_bucket_layout(k0_runs):
    assert_check(k0_runs, "hygiene")


def test_k5_commits_and_probe(k5_runs):
    assert_check(k5_runs, "commits")
    assert_check(k5_runs, "probe")


def test_k5_rollover_does_not_wait(k5_runs):
    assert_check(k5_runs, "checkpoint_prompt")
    assert_check(k5_runs, "hold_engaged")
    assert_check(k5_runs, "acks_prompt")


def test_k5_bucket_after_crash(k5_runs):
    assert_check(k5_runs, "layout_after_crash")


def test_k5_restore_after_crash(k5_runs):
    assert_check(k5_runs, "restore_after_crash")


def test_k5_old_disk_boot(k5_runs):
    assert_check(k5_runs, "stale_boot")
    assert_check(k5_runs, "stale_restore")


def test_k5_old_disk_publishes_nothing_older(k5_runs):
    assert_check(k5_runs, "stale_chain")


def test_k5_bucket_layout(k5_runs):
    assert_check(k5_runs, "hygiene")


def test_t_commits_and_rollover(throttle_runs):
    assert_check(throttle_runs, "commits")
    assert_check(throttle_runs, "checkpoint_prompt")
    assert_check(throttle_runs, "hold_engaged")
    assert_check(throttle_runs, "acks_prompt")


def test_t_acknowledgements_during_burst_are_prompt(throttle_runs):
    assert_check(throttle_runs, "burst_acks")


def test_t_checkpoint_after_burst(throttle_runs):
    assert_check(throttle_runs, "checkpoint_after_burst")


def test_t_commits_after_burst(throttle_runs):
    assert_check(throttle_runs, "acks_after_burst")


def test_t_restore_after_crash(throttle_runs):
    assert_check(throttle_runs, "restore_after_burst")


def test_t_history_is_one_chain(throttle_runs):
    assert_check(throttle_runs, "history")


def test_t_bucket_layout(throttle_runs):
    assert_check(throttle_runs, "hygiene")


@pytest.fixture(scope="module")
def legacy_runs():
    return [execute(scenario_legacy, "L", i, LEGACY_CHECKS) for i in range(REPEATS)]


@pytest.fixture(scope="module")
def rollback_runs():
    return [execute(scenario_rollback, "R", i, ROLLBACK_CHECKS) for i in range(REPEATS)]


@pytest.fixture(scope="module")
def restarted_rollback_runs():
    return [execute(lambda r, c: scenario_rollback(r, c, restarted=True), "RR", i,
                    ROLLBACK_CHECKS) for i in range(REPEATS)]


def test_legacy_restore_of_restart_generation(legacy_runs):
    assert_check(legacy_runs, "legacy_restore")


def test_legacy_boot_and_commits(legacy_runs):
    assert_check(legacy_runs, "legacy_boot")
    assert_check(legacy_runs, "legacy_commits")


def test_legacy_restore_after_crash(legacy_runs):
    assert_check(legacy_runs, "legacy_restore_after_crash")


def test_legacy_generation_links(legacy_runs):
    assert_check(legacy_runs, "legacy_chain")


def test_legacy_bucket_layout(legacy_runs):
    assert_check(legacy_runs, "hygiene")


def test_rollback_remnant_restore(rollback_runs):
    assert_check(rollback_runs, "rollback_restore")


def test_rollback_bucket_layout(rollback_runs):
    assert_check(rollback_runs, "hygiene")


def test_rollback_remnant_behind_restart_snapshot(restarted_rollback_runs):
    assert_check(restarted_rollback_runs, "rollback_restore")
