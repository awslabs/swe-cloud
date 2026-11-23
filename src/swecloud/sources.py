"""Write tasks/SOURCES.md, the list of every task's source, from each task's `[metadata.source]`.

Run `uv run --locked python -m swecloud.sources` after adding or changing a task; tests/test_tasks.py
fails while the file is out of date.
"""

from __future__ import annotations

import tomllib
from collections import Counter

from swecloud import config

PATH = config.TASKS / "SOURCES.md"


def _sources(split: str) -> list[tuple[str, dict]]:
    root = config.split_dir(split)
    return [(task.name, tomllib.loads((task / "task.toml").read_text())["metadata"]["source"]) for task in config.task_dirs(root)]


def render() -> str:
    lines = [
        "# Task sources",
        "",
        "Generated from each task's `[metadata.source]` in `task.toml` by `uv run --locked python -m swecloud.sources`; do not edit by hand.",
        "A `revision` is the upstream commit the task was built from (a task that ports upstream code names the same commit in its `ATTRIBUTION.md`); a `date` is when a write-up was published.",
        "",
        config.canary(),
    ]
    for split, info in config.splits().items():
        rows = _sources(split)
        kinds = Counter(source["kind"] for _, source in rows)
        lines += ["", f"## {info['title']} ({len(rows)} tasks)", ""]
        lines += [", ".join(f"{kind}: {n}" for kind, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))), ""]
        lines += ["| task | kind | source | revision or date |", "| --- | --- | --- | --- |"]
        for name, source in rows:
            when = f"`{source['revision'][:12]}`" if "revision" in source else str(source["date"])
            lines.append(f"| `{name}` | {source['kind']} | [{source['project']}]({source['url']}) | {when} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    PATH.write_text(render())
    print(f"wrote {PATH.relative_to(config.ROOT)}")


if __name__ == "__main__":
    main()
