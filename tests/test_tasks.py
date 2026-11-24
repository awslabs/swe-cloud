"""Release checks on the task sets in tasks/<split>/."""

import os
import re
import tomllib

import pytest
import yaml

from swecloud import config


def _tasks(split):
    return config.task_dirs(config.split_dir(split))


SPLITS = ["issue-fixing", "program-construction"]


@pytest.mark.parametrize("split", SPLITS)
def test_each_split_holds_only_task_folders(split):
    root = config.split_dir(split)
    assert sorted(root.iterdir() if root.is_dir() else []) == _tasks(split)


@pytest.mark.parametrize("split", SPLITS)
def test_every_task_is_complete(split):
    for task in _tasks(split):
        for part in ("task.toml", "instruction.md", "environment/Dockerfile", "tests/test.sh", "solution/solve.sh"):
            assert (task / part).is_file(), f"{task.name}: missing {part}"


@pytest.mark.parametrize("split", SPLITS)
@pytest.mark.skipif(
    bool(os.environ.get("SWECLOUD_NO_LFS")),
    reason="SWECLOUD_NO_LFS is set: a checkout without Git LFS objects on purpose (CI)",
)
def test_task_files_are_fetched_from_git_lfs(split):
    pointers = config.lfs_pointers(config.split_dir(split))
    assert not pointers, f"{len(pointers)} file(s) are Git LFS pointers, e.g. {pointers[0]}; run `git lfs pull`"


@pytest.mark.parametrize("split", SPLITS)
def test_tasks_carry_the_split_prefix_and_type(split):
    expected = split.replace("-", "_")
    for task in _tasks(split):
        toml = tomllib.loads((task / "task.toml").read_text())
        assert toml["task"]["name"] == "swe-cloud/" + task.name, task
        assert task.name.startswith(config.splits()[split]["alias"] + "-"), task
        assert toml["metadata"]["task_type"] == expected, task


@pytest.mark.parametrize("split", SPLITS)
def test_tasks_grade_a_copy_of_app_in_a_separate_container(split):
    for task in _tasks(split):
        toml = tomllib.loads((task / "task.toml").read_text())
        assert toml["verifier"]["environment_mode"] == "separate", task
        assert "/app" in toml["artifacts"], task


@pytest.mark.parametrize("split", SPLITS)
def test_tasks_have_no_network_while_the_agent_works_or_is_graded(split):
    """The agent and verifier phases are offline. The `[environment]` baseline may be public, so a sandbox
    such as Modal can fetch packages while it builds the image; under Docker each task's compose file keeps
    its containers offline throughout (the tests below)."""
    for task in _tasks(split):
        toml = tomllib.loads((task / "task.toml").read_text())
        assert toml["agent"]["network_mode"] == "no-network", task
        assert toml["verifier"]["network_mode"] == "no-network", task
        assert toml["environment"]["network_mode"] in ("no-network", "public"), task


@pytest.mark.parametrize("split", SPLITS)
def test_multi_service_tasks_keep_their_network_internal(split):
    """A task with several services talks over its own network, which must have no route out.

    The network must be declared, with every service on it (or sharing another service's namespace
    through `network_mode`): left undeclared, Harbor enforces no-network by moving `main` behind its
    egress sidecar, where it cannot reach the other services, and the task cannot start.
    """
    for task in _tasks(split):
        for compose in (task / "environment" / "docker-compose.yaml", task / "tests" / "docker-compose.yaml"):
            if not compose.exists():
                continue
            spec = yaml.safe_load(compose.read_text())
            services = spec.get("services") or {}
            if len(services) < 2:
                continue
            where = compose.relative_to(task.parent)
            networks = spec.get("networks") or {}
            assert networks, f"{where}: several services but no network declared"
            for name, net in networks.items():
                net = net or {}
                # Sealed either way: an internal network has no gateway; a bridge without
                # masquerading has no NAT to the host's network.
                no_nat = (net.get("driver_opts") or {}).get("com.docker.network.bridge.enable_ip_masquerade") == "false"
                assert net.get("internal") is True or no_nat, f"{where}: network {name} has a route out"
            for name, service in services.items():
                service = service or {}
                if "network_mode" in service:
                    continue
                joined = service.get("networks") or []
                assert joined, f"{where}: service {name} is on no declared network"
                assert set(joined) <= set(networks), f"{where}: service {name} joins an undeclared network"


@pytest.mark.parametrize("split", SPLITS)
def test_single_container_tasks_carry_the_hardening_compose_file(split):
    template = (config.ROOT / "sandbox" / "docker-hardening.yaml").read_text()
    for task in _tasks(split):
        compose = task / "environment" / "docker-compose.yaml"
        services = yaml.safe_load(compose.read_text()).get("services", {}) if compose.exists() else {}
        if len(services) < 2:
            assert compose.exists() and compose.read_text() == template, f"{task.name}: missing or changed hardening"


@pytest.mark.parametrize("split", SPLITS)
def test_multi_service_tasks_harden_the_agents_container(split):
    """`main` in a venue gets the single-container hardening, except its network; it may add only NET_ADMIN."""
    hardening = yaml.safe_load((config.ROOT / "sandbox" / "docker-hardening.yaml").read_text())["services"]["main"]
    weak = []
    for task in _tasks(split):
        for compose in (task / "environment" / "docker-compose.yaml", task / "tests" / "docker-compose.yaml"):
            services = (yaml.safe_load(compose.read_text()).get("services") or {}) if compose.exists() else {}
            if len(services) < 2:
                continue
            main = services.get("main") or {}
            where = compose.relative_to(task.parent)
            if not set(hardening["security_opt"]) <= set(main.get("security_opt") or []):
                weak.append(f"{where}: security_opt")
            if not 0 < (main.get("pids_limit") or 0) <= hardening["pids_limit"]:
                weak.append(f"{where}: pids_limit")
            if not set(hardening["cap_drop"]) <= set(main.get("cap_drop") or []):
                weak.append(f"{where}: cap_drop")
            if main.get("privileged") or not set(main.get("cap_add") or []) <= {"NET_ADMIN"}:
                weak.append(f"{where}: privileged or extra capabilities")
    assert not weak, f"{len(weak)} venue main service(s) lack hardening: {weak[:5]}"


def test_each_split_holds_the_release_task_count():
    for split in SPLITS:
        expected = config.splits()[split]["tasks"]
        assert len(_tasks(split)) == expected, f"tasks/{split}/ holds {len(_tasks(split))} tasks; tasks/manifest.toml says {expected}"


def test_the_readmes_count_the_tasks_in_the_repository():
    counts = {split: len(_tasks(split)) for split in SPLITS}
    readme = (config.ROOT / "README.md").read_text()
    assert f"badge/tasks-{sum(counts.values())}-" in readme, "README.md: tasks badge"
    total, fixing, construction = sum(counts.values()), counts["issue-fixing"], counts["program-construction"]
    assert f"pass@1 over all {total} tasks: {fixing} issue-fixing (IF) and {construction} program-construction (PC)" in readme, "README.md: Leaderboard"
    assert f"| Issue fixing | {fixing} |" in readme, "README.md: Tasks (issue fixing)"
    assert f"| Program construction | {construction} |" in readme, "README.md: Tasks (program construction)"
    listing = (config.TASKS / "README.md").read_text()
    for split, n in counts.items():
        assert f"| `{split}` | `{config.splits()[split]['alias']}` | {n} |" in listing, f"tasks/README.md: {split}"
    changelog = (config.ROOT / "CHANGELOG.md").read_text()
    for split, n in counts.items():
        assert f"{n} {split} tasks in `tasks/{split}/`" in changelog, f"CHANGELOG.md: {split}"


def _removes_reward_files_before_grading(task) -> bool:
    """Release check 10: the verifier deletes reward.txt and reward.json before it runs the tests."""
    if (task / "tests" / "verify.py").exists():  # program-construction: verify.py runs pytest itself
        text = (task / "tests" / "verify.py").read_text()
        removal = re.search(r"for name in \([^)]*\"reward\.txt\"[^)]*\"reward\.json\"[^)]*\):\s*\n\s*\(logs / name\)\.unlink", text)
        grading = text.find("pytest")
        return bool(removal) and 0 <= removal.start() < grading
    lines = (task / "tests" / "test.sh").read_text().splitlines()
    removal = next((i for i, line in enumerate(lines) if re.search(r"\brm -f\b.*reward\.txt.*reward\.json|\brm -f\b.*reward\.json.*reward\.txt", line)), None)
    grading = next((i for i, line in enumerate(lines) if "pytest" in line), len(lines))
    return removal is not None and removal < grading


@pytest.mark.parametrize("split", SPLITS)
def test_verifiers_remove_old_reward_files_before_grading(split):
    missing = [task.name for task in _tasks(split) if not _removes_reward_files_before_grading(task)]
    assert not missing, f"{len(missing)} task(s) do not remove reward.txt and reward.json before the tests: {missing[:5]}"


# The emulator image that compose files may name by tag (some sandbox providers reject digests there); docs/TASKS.md.
COMPOSE_TAGGED_IMAGES = {"floci/floci:2.2.0"}


@pytest.mark.parametrize("split", SPLITS)
def test_images_are_pinned_by_digest(split):
    unpinned = []
    for task in _tasks(split):
        for dockerfile in task.rglob("Dockerfile"):
            text = dockerfile.read_text()
            images = [line.split()[1] for line in text.splitlines() if line.startswith("FROM ")]
            images += re.findall(r"crane (?:pull|mutate)\s+(?:--platform \S+\s+)?(\S+)", text)
            unpinned += [f"{dockerfile.relative_to(task.parent)}: {image}" for image in images if "@sha256:" not in image]
        for compose in task.rglob("docker-compose.yaml"):
            for service in (yaml.safe_load(compose.read_text()).get("services") or {}).values():
                image = (service or {}).get("image")
                if image and "@sha256:" not in image and image not in COMPOSE_TAGGED_IMAGES:
                    unpinned.append(f"{compose.relative_to(task.parent)}: {image}")
    assert not unpinned, f"{len(unpinned)} image reference(s) without a digest: {unpinned[:5]}"


def _attribution_problems(task) -> list[str]:
    """Release check 7: ATTRIBUTION.md names the upstream project, its exact revision, and its licenses."""
    text = (task / "ATTRIBUTION.md").read_bytes().decode("utf-8", "replace").replace("\r", "")
    problems = []
    if not re.search(r"^Upstream: https://\S+$", text, re.M):
        problems.append("no `Upstream:` URL")
    # A task that also ports a second library credits it with its own `Revision:` line.
    revisions = re.findall(r"^Revision: (.*)$", text, re.M)
    if not revisions:
        problems.append("no `Revision:`")
    problems += [f"revision {r!r} is not a commit" for r in revisions if not re.fullmatch(r"[0-9a-f]{7,40}", r.strip())]
    if not re.search(r"^## \S", text, re.M):
        problems.append("no license section")
    return problems


@pytest.mark.parametrize("split", SPLITS)
def test_attributions_name_the_upstream_revision_and_licenses(split):
    bad = []
    for task in _tasks(split):
        if not (task / "ATTRIBUTION.md").exists():
            if split == "program-construction":  # every program-construction task reproduces an open-source program
                bad.append(f"{task.name}: no ATTRIBUTION.md")
            continue
        bad += [f"{task.name}: {problem}" for problem in _attribution_problems(task)]
    assert not bad, f"{len(bad)} attribution problem(s): {bad[:5]}"


SOURCE_KINDS = {"issue", "pull-request", "program", "design", "article", "paper", "postmortem"}  # docs/TASKS.md


def _source_problems(task) -> list[str]:
    """The task's `[metadata.source]`: kind, project, link, and the commit or date it was taken at."""
    source = tomllib.loads((task / "task.toml").read_text())["metadata"].get("source")
    if not isinstance(source, dict):
        return ["no [metadata.source]"]
    problems = []
    if source.get("kind") not in SOURCE_KINDS:
        problems.append(f"kind {source.get('kind')!r}")
    if task.name.startswith("pc-") and source.get("kind") != "program":
        problems.append("a program-construction source is not kind `program`")
    if not str(source.get("project") or "").strip():
        problems.append("no project")
    if not re.fullmatch(r"https://\S+", str(source.get("url") or "")):
        problems.append("no https url")
    revision, date = source.get("revision"), source.get("date")
    if (revision is None) == (date is None):
        problems.append("needs exactly one of revision and date")
    elif revision is not None and not re.fullmatch(r"[0-9a-f]{7,40}", str(revision)):
        problems.append(f"revision {revision!r} is not a commit")
    elif date is not None and not re.fullmatch(r"\d{4}(-\d{2}){0,2}", str(date)):
        problems.append(f"date {date!r} is not YYYY, YYYY-MM or YYYY-MM-DD")
    attribution = task / "ATTRIBUTION.md"
    if revision is not None and attribution.exists():
        if str(revision) not in re.findall(r"^Revision: (\S+)", attribution.read_bytes().decode("utf-8", "replace"), re.M):
            problems.append("revision differs from ATTRIBUTION.md")
    return problems


@pytest.mark.parametrize("split", SPLITS)
def test_tasks_record_their_source(split):
    bad = [f"{task.name}: {problem}" for task in _tasks(split) for problem in _source_problems(task)]
    assert not bad, f"{len(bad)} source metadata problem(s): {bad[:5]}"


def test_the_sources_list_is_up_to_date():
    from swecloud import sources

    assert sources.PATH.read_text() == sources.render(), "tasks/SOURCES.md is stale: run `uv run --locked python -m swecloud.sources`"


def test_the_task_sources_have_known_kinds():
    from collections import Counter

    kinds = {split: Counter(tomllib.loads((t / "task.toml").read_text())["metadata"]["source"]["kind"] for t in _tasks(split)) for split in SPLITS}
    fixing, n_fixing, n_construction = kinds["issue-fixing"], len(_tasks("issue-fixing")), len(_tasks("program-construction"))
    assert sum(fixing.values()) == n_fixing and set(fixing) <= {"issue", "pull-request", "design", "article", "postmortem", "paper"}
    assert kinds["program-construction"] == Counter(program=n_construction)


# Reference programs still dynamically linked; EVALUATION_PROTOCOL.md §7 lists them as a known limitation.
DYNAMIC_REFERENCE_PROGRAMS = {
    "pc-cloudwatch-otlp-metrics-forwarder",  # a static rebuild from the pinned source would change its behavior
    "pc-msk-rds-batch-sql-stream-processor",  # a bundled Python application
    "pc-rds-background-job-broker",  # C++ with MariaDB, libevent and Boost
    "pc-rds-parallel-database-cloner",  # C with libpq and libgc
}


def _has_interpreter(program) -> bool:
    """True if the ELF file asks for a dynamic loader (a PT_INTERP program header)."""
    data = program.read_bytes()
    assert data[:4] == b"\x7fELF" and data[4] == 2, f"{program}: not a 64-bit ELF file"
    phoff, phentsize, phnum = int.from_bytes(data[32:40], "little"), int.from_bytes(data[54:56], "little"), int.from_bytes(data[56:58], "little")
    return any(int.from_bytes(data[phoff + i * phentsize : phoff + i * phentsize + 4], "little") == 3 for i in range(phnum))


@pytest.mark.skipif(
    bool(os.environ.get("SWECLOUD_NO_LFS")),
    reason="SWECLOUD_NO_LFS is set: a checkout without Git LFS objects on purpose (CI)",
)
def test_reference_programs_are_statically_linked():
    """Release check 12: a dynamically linked reference would load code named in LD_PRELOAD, which could copy it."""
    dynamic = {t.name for t in _tasks("program-construction") if _has_interpreter(t / "environment" / "reference" / "program")}
    assert not dynamic - DYNAMIC_REFERENCE_PROGRAMS, f"dynamically linked reference programs: {sorted(dynamic - DYNAMIC_REFERENCE_PROGRAMS)}"
    assert not DYNAMIC_REFERENCE_PROGRAMS - dynamic, f"now static, remove from DYNAMIC_REFERENCE_PROGRAMS: {sorted(DYNAMIC_REFERENCE_PROGRAMS - dynamic)}"
