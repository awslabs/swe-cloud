"""Host-side handling of the folders Harbor mounts into the sandbox, and the egress probe's sandbox."""

import json
from pathlib import Path

import pytest
import yaml

from swecloud import cli, config, score
from swecloud.agents import terminus


def test_trajectory_write_replaces_a_symlink_instead_of_following_it(tmp_path):
    host_file = tmp_path / "bashrc"
    host_file.write_text("original\n")
    logs = tmp_path / "agent"
    logs.mkdir()
    (logs / "trajectory.json").symlink_to(host_file)

    with terminus._trajectory_writes_replace_files():
        with terminus._terminus_2.open(logs / "trajectory.json", "w") as f:
            f.write("{}")

    assert host_file.read_text() == "original\n"
    assert not (logs / "trajectory.json").is_symlink()
    assert (logs / "trajectory.json").read_text() == "{}"
    assert sorted(p.name for p in logs.iterdir()) == ["trajectory.json"]
    assert "open" not in vars(terminus._terminus_2)


def test_failed_write_leaves_no_temp_file(tmp_path):
    with pytest.raises(RuntimeError):
        with terminus._replace_on_close(tmp_path / "trajectory.json", "w") as f:
            f.write("partial")
            raise RuntimeError
    assert list(tmp_path.iterdir()) == []


def test_score_refuses_symlinks_in_trial_folders(tmp_path):
    job = tmp_path / "job"
    (job / "t__1" / "agent").mkdir(parents=True)
    (job / "t__1" / "agent" / "trajectory.json").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(SystemExit):
        score.trials(job)


def _trial(job: Path, name: str = "t__1") -> Path:
    trial = job / name
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"task_name": "swe-cloud/pc-x", "trial_name": name}))
    (trial / "trial.log").write_text("")
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": []}))
    return trial


@pytest.mark.parametrize("path", ["result.json", "trial.log", "agent/trajectory.json", "agent"])
def test_score_refuses_a_symlink_on_a_path_it_reads(tmp_path, path):
    host = tmp_path / "host"
    host.mkdir()
    (host / "trajectory.json").write_text(json.dumps({"steps": []}))
    trial = _trial(tmp_path / "job")
    target = trial / path
    if target.is_dir():
        for child in target.iterdir():
            child.unlink()
        target.rmdir()
        target.symlink_to(host, target_is_directory=True)
    else:
        target.unlink()
        target.symlink_to(host / "trajectory.json")
    with pytest.raises(SystemExit, match="symbolic link"):
        score.trials(tmp_path / "job")


def test_score_refuses_a_trial_folder_that_is_a_symlink(tmp_path):
    elsewhere = _trial(tmp_path / "elsewhere")
    (tmp_path / "job").mkdir()
    (tmp_path / "job" / "t__2").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(SystemExit, match="symbolic link"):
        score.trials(tmp_path / "job")


def test_score_and_cleanup_accept_a_symlink_in_the_agents_app(tmp_path):
    # An agent may provide /app/program as a link to its script; Harbor copies /app into artifacts/app.
    app = _trial(tmp_path / "job") / "artifacts" / "app"
    app.mkdir(parents=True)
    (app / "main.py").write_text("print(1)\n")
    (app / "program").symlink_to("main.py")
    assert [row["trial"] for row in score.trials(tmp_path / "job")] == ["t__1"]
    score.check_job_dir(tmp_path / "job", reads=(), command="cleanup")


def test_cleanup_checks_only_the_trial_folders(tmp_path):
    trial = _trial(tmp_path / "job")
    (trial / "trial.log").unlink()
    (trial / "trial.log").symlink_to(tmp_path / "elsewhere")
    score.check_job_dir(tmp_path / "job", reads=(), command="cleanup")  # cleanup reads no file in a trial
    (tmp_path / "job" / "t__2").symlink_to(trial, target_is_directory=True)
    with pytest.raises(SystemExit, match="`cleanup` reads"):
        score.check_job_dir(tmp_path / "job", reads=(), command="cleanup")


def test_egress_probe_has_no_network_interface():
    probe = config.ROOT / "probes" / "egress-sealed"
    compose = (probe / "environment" / "docker-compose.yaml").read_text()
    # The probe tests the single-container hardening itself, so it carries an unchanged copy of the template.
    assert compose == (config.ROOT / "sandbox" / "docker-hardening.yaml").read_text()
    main = yaml.safe_load(compose)["services"]["main"]
    assert main["network_mode"] == "none"
    assert "no-new-privileges:true" in main["security_opt"]
    commands = [l for l in (probe / "tests" / "test.sh").read_text().splitlines() if l and not l.startswith("#")]
    assert commands[0] == "rm -f /logs/verifier/reward.txt /logs/verifier/reward.json"


@pytest.mark.parametrize(
    ("result", "code", "says"),
    [
        ({"verifier_result": {"rewards": {"reward": 1}}}, 0, "is sealed"),
        ({"verifier_result": {"rewards": {"reward": 0}}}, 1, "NOT sealed"),
        (
            {"exception_info": {"exception_type": "RuntimeError", "exception_message": "nft: Could not process rule\nmore"}},
            2, "the probe failed (RuntimeError: nft: Could not process rule)",
        ),
        (None, 2, "the probe failed (harbor exited 1 without a trial result)"),
    ],
)
def test_egress_check_tells_a_failed_probe_from_an_unsealed_sandbox(tmp_path, result, code, says):
    if result is not None:
        (tmp_path / "egress-sealed__1").mkdir()
        (tmp_path / "egress-sealed__1" / "result.json").write_text(
            json.dumps({"task_name": "egress-sealed", "trial_name": "egress-sealed__1", **result})
        )
    got_code, message = cli._egress_verdict(tmp_path, returncode=0 if result else 1)
    assert got_code == code
    assert says in message


def test_egress_check_reports_a_probe_job_that_was_never_created(tmp_path):
    # Harbor can fail before it creates the job folder; that is a failed probe, not a crash.
    got_code, message = cli._egress_verdict(tmp_path / "egress-probe-never-created", returncode=1)
    assert got_code == 2
    assert "the probe failed" in message
