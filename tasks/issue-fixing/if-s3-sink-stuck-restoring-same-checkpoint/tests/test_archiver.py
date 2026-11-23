"""The archiver job through restarts: the same checkpoint restored repeatedly, a savepoint, a
superseded fresh start and a superseded restore cancelled while the next attempt runs, a superseded
attempt that resumes its startup while the next attempt runs, a checkpoint that never completed, a
late completion notification, a resubmission by hand after the last attempt's host was lost, a
lost final notification."""


def _context(run):
    return f"errors={run.errors} restores={run.restores}"


def test_every_restore_succeeds(run):
    expected = {"a3": run.k(4), "a4": run.k(4), "a6": run.k(4), "s2a0": run.k(7)}
    got = {a: run.restores.get(a, (cid, "not attempted"))[1] for a, cid in expected.items()}
    assert got == {a: str(cid) for a, cid in expected.items()}, _context(run)


def test_schedule_runs_to_the_end(run):
    assert run.completed_schedule and not run.errors, _context(run)


def test_retained_checkpoints_and_savepoint_stay_restorable(run):
    failing = [(label, problems) for label, problems in run.audits if problems]
    assert not failing, failing[:3]


def test_superseded_attempts_leave_running_attempt_intact(run):
    assert len(run.cancel_checks) == 2, _context(run)
    assert not any(run.cancel_checks.values()), run.cancel_checks


def test_no_unreferenced_objects_after_run(run):
    assert run.completed_schedule, _context(run)
    assert run.final["stray_objects"] == [], run.final["stray_objects"]
    assert run.final["foreign_objects"] == [], run.final["foreign_objects"]
    assert run.final["missing_tails"] == [], run.final["missing_tails"]


def test_no_unreferenced_uploads_after_run(run):
    assert run.completed_schedule, _context(run)
    assert run.final["stray_uploads"] == [], run.final["stray_uploads"]


def test_deletes_are_real_deletes(run):
    assert run.completed_schedule, _context(run)
    assert run.final["hidden_versions"] == [], run.final["hidden_versions"]


def test_other_job_untouched(run):
    assert run.completed_schedule, _context(run)
    assert run.final["other_job"] == [], run.final["other_job"]


def test_published_part_file_is_the_stream(run):
    assert run.completed_schedule, _context(run)
    assert run.final.get("published_ok") is True, run.final.get("published_len")
