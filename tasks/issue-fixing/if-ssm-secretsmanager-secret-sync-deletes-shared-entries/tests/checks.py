"""Small assertion helpers that collect every problem at a checkpoint instead of stopping at the first."""

from __future__ import annotations


class Problems(list):
    def add(self, msg: str):
        self.append(msg)

    def verdict(self, step) -> str:
        return "\n".join(["- " + p for p in self] + ["", step.run.describe()])


def run_basics(p: Problems, step, expect_rc: int | None):
    run = step.run
    if not run.requests:
        p.add("syncctl made no AWS request through AWS_ENDPOINT_URL (it must use the standard SDK endpoint "
              "configuration)")
    if run.timed_out:
        p.add("syncctl did not exit within the invocation timeout")
    if expect_rc is not None and run.returncode != expect_rc:
        p.add(f"exit status {run.returncode}, expected {expect_rc}")
    if run.swept:
        # Not graded on its own: a fresh pod simply never sees these files again.
        pass


def unchanged(p: Problems, state: dict, before: dict, names, what: str):
    for name in sorted(names):
        if name not in before:
            continue
        if name not in state:
            p.add(f"{what} {name} was deleted")
        elif state[name] != before[name]:
            p.add(f"{what} {name} was modified: before={before[name]} after={state[name]}")


def has_values(p: Problems, state: dict, expected: dict[str, str], conf_tags: dict[str, str], what: str):
    for name, value in sorted(expected.items()):
        rec = state.get(name)
        if rec is None:
            p.add(f"{what} {name} is missing")
            continue
        if rec.get("deleted"):
            p.add(f"{what} {name} is pending deletion")
            continue
        if rec.get("value") != value:
            p.add(f"{what} {name} has value {rec.get('value')!r}, expected {value!r}")
        for k, v in conf_tags.items():
            if rec.get("tags", {}).get(k) != v:
                p.add(f"{what} {name} lacks configured tag {k}={v} (tags={rec.get('tags')})")


def exact_names(p: Problems, state: dict, expected: set[str]):
    present = set(state)
    for name in sorted(present - expected):
        extra = " (pending deletion: deletes must be immediate)" if state[name].get("deleted") else ""
        p.add(f"unexpected entry {name}{extra}")
    for name in sorted(expected - present):
        p.add(f"missing entry {name}")


def hand_at(tl, label: str) -> dict:
    """Hand-managed entries as of step ``label``: the seeds, plus what people made by hand up to it."""
    order = list(tl.steps)
    upto = order[: order.index(label) + 1]
    late = {name: e["rec"] for name, e in tl.facts.get("late_hand", {}).items() if e["from"] in upto}
    return {**tl.seeds, **late}
