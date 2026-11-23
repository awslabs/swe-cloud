"""Scoring rules and harness_events on a synthetic job folder."""

import json

from swecloud import score


def _trial(job, name, reward, exception=None, steps=(), log="", meta=None):
    d = job / name
    (d / "agent").mkdir(parents=True)
    (d / "result.json").write_text(json.dumps({
        "task_name": name.split("__")[0], "trial_name": name,
        "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
        "exception_info": {"exception_type": exception} if exception else None,
        "agent_result": {"cost_usd": 1.0, "metadata": meta or {}},
    }))
    (d / "agent" / "trajectory.json").write_text(json.dumps({"steps": [{"source": "user"}, *steps]}))
    (d / "trial.log").write_text(log)


def test_scoring_and_events(tmp_path):
    cmd = {"source": "agent", "message": "ok", "tool_calls": [{"arguments": {"keystrokes": "ls\n"}}],
           "metrics": {"completion_tokens": 5}}
    empty = {"source": "agent", "message": "", "metrics": {"completion_tokens": 900}}
    thinking_only = {"source": "agent", "message": "", "reasoning_content": "thought", "metrics": {"completion_tokens": 50}}
    done_no_cmd = {"source": "agent", "message": "done", "tool_calls": [{"arguments": {}}],
                   "metrics": {"completion_tokens": 5}}
    _trial(tmp_path, "a__1", 1.0, steps=[cmd])
    _trial(tmp_path, "a__2", 1.0, exception="AgentTimeoutError", steps=[cmd])  # timeout: fails
    _trial(tmp_path, "b__1", 0.0, steps=[empty, thinking_only, cmd], log="Output length exceeded: ...\n")
    _trial(tmp_path, "c__1", 0.0, steps=[done_no_cmd],
           meta={"harness_content_filter_declines": 4, "harness_max_tokens_truncations": 1})
    s = score.summarize(score.trials(tmp_path))
    assert s["passed_trials"] == 1
    assert s["pass@1"] == (0.5 + 0 + 0) / 3
    assert s["timeouts_with_reward_1"] == 1
    ev = s["harness_events"]
    assert ev["content_filter_declines"] == 4
    assert ev["empty_replies"] == 1 and ev["trials_with_empty_replies"] == 1
    assert ev["thinking_only_replies"] == 1
    assert ev["max_tokens_truncations"] == 2  # one from trial.log, one from metadata
    assert ev["trials_without_commands"] == 1


def test_almost_at_1_and_the_swe_cloud_score(tmp_path):
    fixing, building = tmp_path / "if-job", tmp_path / "pc-job"
    def trial(job, task, n, reward, exception=None):
        d = job / f"{task.split('/')[-1]}__{n}"
        (d / "agent").mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({
            "task_name": task, "trial_name": d.name, "verifier_result": {"rewards": {"reward": reward}},
            "exception_info": {"exception_type": exception} if exception else None, "agent_result": {"cost_usd": 1.0},
        }))
    trial(fixing, "swe-cloud/if-a", 1, 1.0); trial(fixing, "swe-cloud/if-a", 2, 0.0)
    trial(fixing, "swe-cloud/if-b", 1, 0.0); trial(fixing, "swe-cloud/if-b", 2, 0.0)
    trial(building, "swe-cloud/pc-a", 1, 0.97); trial(building, "swe-cloud/pc-a", 2, 0.5)
    trial(building, "swe-cloud/pc-b", 1, 1.0); trial(building, "swe-cloud/pc-b", 2, 0.99, exception="AgentTimeoutError")
    pc = score.summarize(score.trials(building))
    assert pc["pass@1"] == (0 + 0.5) / 2
    assert pc["almost@1"] == (0.5 + 0.5) / 2  # 0.97 counts, 0.5 does not, a timeout never counts
    combined = score.swe_cloud_score(score.trials(fixing) + score.trials(building))
    assert combined["issue-fixing"]["pass@1"] == 0.25
    assert combined["program-construction"]["pass@1"] == 0.25
    assert abs(combined["score"] - (0.6 * 0.25 + 0.4 * 0.25)) < 1e-12
    assert score.swe_cloud_score(score.trials(fixing)) is None  # needs both splits


def test_parse_errors_and_idle_replies_are_counted(tmp_path):
    cmd = {"source": "agent", "message": "ok", "tool_calls": [{"arguments": {"keystrokes": "ls\n"}}],
           "metrics": {"completion_tokens": 5}}
    unparsed = {"source": "agent", "message": "<invoke name=\"bash\">ls</invoke>", "metrics": {"completion_tokens": 9},
                "observation": {"results": [{"content": "Previous response had parsing errors:\nERROR: No valid JSON found"}]}}
    idle = {"source": "agent", "message": "The task remains incomplete.", "metrics": {"completion_tokens": 9},
            "observation": {"results": [{"content": "New Terminal Output:\n"}]}}
    done = {"source": "agent", "message": "done", "tool_calls": [{"function_name": "mark_task_complete", "arguments": {}}],
            "metrics": {"completion_tokens": 5}}
    _trial(tmp_path, "a__1", 0.0, steps=[cmd, unparsed, unparsed, idle, idle, idle, done])
    ev = score.summarize(score.trials(tmp_path))["harness_events"]
    assert ev["parse_error_replies"] == 2
    assert ev["replies_without_commands"] == 3  # the finishing call and the unparsed replies are not counted


def _manifest(job, argv, effort="xhigh"):
    (job.parent / f"{job.name}.manifest.json").write_text(json.dumps({"harbor_argv": argv, "effort": effort}))


def test_score_flags_a_job_that_left_the_protocol(tmp_path):
    from pathlib import Path
    from swecloud import config
    job = tmp_path / "job"
    job.mkdir()
    base = config.RunSpec(profile="bedrock-docker", model="global.example.model-v1", tasks=Path("tasks/issue-fixing"),
                          job_name="job", jobs_dir=tmp_path, n_concurrent=2)
    _manifest(job, base.argv())
    assert score.protocol_check(job) == {"under_protocol": True, "deviations": [], "effort": "xhigh"}
    _manifest(job, base.argv(), effort="high")
    assert score.protocol_check(job)["under_protocol"] is True  # a listed effort is still the protocol
    changed = config.RunSpec(**{**base.__dict__, "n_attempts": 1, "extra": ("--max-retries", "5", "--debug")})
    _manifest(job, changed.argv())
    check = score.protocol_check(job)
    assert check["under_protocol"] is False
    assert "--n-attempts 1 (protocol: 3)" in check["deviations"]
    assert "--max-retries 5 (protocol: 1)" in check["deviations"]
    assert any("--debug" in d for d in check["deviations"])
    (tmp_path / "job.manifest.json").unlink()
    assert score.protocol_check(job)["under_protocol"] is None


def test_task_versions_tell_runs_on_different_task_files_apart(tmp_path):
    def job(name, checksums):
        d = tmp_path / name
        for i, (task, checksum) in enumerate(checksums):
            t = d / f"{task.split('/')[-1]}__{i}"
            (t / "agent").mkdir(parents=True)
            (t / "result.json").write_text(json.dumps({"task_name": task, "trial_name": t.name, "task_checksum": checksum,
                                                       "verifier_result": {"rewards": {"reward": 1.0}}}))
        return score.summarize(score.trials(d))["task_versions"]
    a = job("a", [("swe-cloud/if-x", "c1"), ("swe-cloud/if-y", "c2")])
    b = job("b", [("swe-cloud/if-y", "c2"), ("swe-cloud/if-x", "c1")])
    c = job("c", [("swe-cloud/if-x", "c1"), ("swe-cloud/if-y", "c3")])
    mixed = job("d", [("swe-cloud/if-x", "c1"), ("swe-cloud/if-x", "c9")])
    assert a["digest"] == b["digest"] != c["digest"]
    assert a["tasks_with_several_versions"] == [] and mixed["tasks_with_several_versions"] == ["swe-cloud/if-x"]
