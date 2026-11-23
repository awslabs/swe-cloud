"""Verifier for the delivery pipeline replacement drill.

Each trial provisions its own streams and state table, runs a worker that is
killed while one message is in flight, starts a replacement on the same
partition, releases the rest of the input, and then grades what the output
stream and the state table hold. The trials differ only in what the killed
worker had managed to do with the in-flight message; the replacement is told
nothing about which trial it is in.
"""

from __future__ import annotations

import json
import os
import random
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from drill import Trial  # noqa: E402
from harness import client, detail, warm  # noqa: E402

VARIANTS = ("before_append", "committed_append", "unwritten_append")
REWARD = "/logs/verifier/reward.txt"
ENVIRONMENT_MARKER = "/logs/verifier/environment.txt"


def _write(path, text):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        pass


@pytest.fixture(scope="module")
def live():
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
    try:
        client("kinesis", endpoint).list_streams()
    except Exception as error:
        _write(REWARD, "0\n")
        _write(ENVIRONMENT_MARKER, "emulator not reachable at %s: %s\n" % (endpoint, error))
        pytest.fail("the emulator is not reachable: %s" % error, pytrace=False)
    if not warm(endpoint):
        _write(REWARD, "0\n")
        _write(ENVIRONMENT_MARKER, "emulator services never answered at %s\n" % endpoint)
        pytest.fail("the emulator's services never answered", pytrace=False)
    return endpoint


@pytest.fixture(scope="module")
def drills(live):
    order = list(VARIANTS)
    random.shuffle(order)
    detail("=== replacement drill, trial order: %s" % ", ".join(order))
    outcomes = {}
    for variant in order:
        result = Trial(variant, live).run()
        if result["fixture"] and not result["events"] and result["phase"] in (
            "provision",
            "unexpected",
        ):
            # The drill itself broke before the worker was ever started. That
            # says nothing about the worker, so the drill is run once more
            # rather than read as an outcome.
            detail("trial %s (%s) broke in setup (%s); running it once more"
                   % (result["run_id"], variant, result["reason"]))
            result = Trial(variant, live).run()
        outcomes[variant] = result
        counts = {key: value for key, value in result["counts"].items()}
        detail(
            "trial %s (%s): ok=%s phase=%s reason=%s counts=%s checkpoint=%s first_seen=%s"
            % (
                result["run_id"],
                variant,
                result["ok"],
                result["phase"],
                result["reason"] or "-",
                counts,
                result["final_checkpoint"],
                result["target_first_seen"],
            )
        )
    _write_summary(outcomes)
    return outcomes


def _write_summary(outcomes):
    summary = {}
    for variant, trial in outcomes.items():
        summary[variant] = {
            "run_id": trial["run_id"],
            "ok": trial["ok"],
            "phase": trial["phase"],
            "reason": trial["reason"],
            "fixture": trial["fixture"],
            "target_id": trial["target_id"],
            "target_records": trial["counts"].get(trial["target_id"], 0),
            "target_first_seen": trial["target_first_seen"],
            "final_checkpoint": trial["final_checkpoint"],
            "quiesced": trial["quiesced"],
            "rows": trial["rows"],
            "data_records": [
                {
                    "messageId": record.get("messageId"),
                    "payload": record.get("payload"),
                    "sequence": record.get("sequence"),
                }
                for record in trial["records"]
                if record.get("kind") == "DATA"
            ],
            "worker_events": trial["events"],
            "worker_stderr": trial["stderr"],
            "timeline": trial["timeline"],
        }
    _write(
        "/logs/verifier/trials.json",
        json.dumps(summary, indent=2, default=str) + "\n",
    )


def _trial(outcomes, variant):
    trial = outcomes[variant]
    assert trial["ok"], (
        "the %s trial did not run to its scan (phase %s: %s)\nworker output:\n%s"
        % (
            variant,
            trial["phase"],
            trial["reason"],
            "\n".join("  %s" % json.dumps(event) for event in trial["events"][-30:])
            or "  (nothing)",
        )
    )
    return trial


def _records_of(trial, message_id):
    return [
        record
        for record in trial["records"]
        if record.get("kind") == "DATA" and record.get("messageId") == message_id
    ]


def _events_of(trial, name):
    return [event for event in trial["events"] if event.get("event") == name]


def test_replacement_does_not_deliver_a_replay_twice(drills):
    """A message the append service had already taken from this partition is
    not appended again, even though the partition's checkpoint never moved
    past it."""
    for variant in ("committed_append", "unwritten_append"):
        trial = _trial(drills, variant)
        target = trial["target_id"]
        found = _records_of(trial, target)
        assert len(found) == 1, (
            "%s trial: expected exactly 1 output record for %r, found %d:\n%s"
            % (
                variant,
                target,
                len(found),
                "\n".join(
                    "  %s %s" % (record.get("sequence"), record.get("payload"))
                    for record in found
                )
                or "  (none)",
            )
        )


def test_replacement_delivers_a_message_that_was_only_recorded(drills):
    """A message whose row is durable but which never reached the output
    stream is delivered, and within the recovery bound."""
    trial = _trial(drills, "before_append")
    target = trial["target_id"]
    found = _records_of(trial, target)
    assert len(found) == 1, (
        "expected exactly 1 output record for %r, found %d:\n%s"
        % (
            target,
            len(found),
            "\n".join(
                "  %s %s" % (record.get("sequence"), record.get("payload"))
                for record in found
            )
            or "  (none)",
        )
    )
    first_seen = trial["target_first_seen"]
    assert first_seen is not None and first_seen <= float(
        trial["recovery_deadline"]
    ), (
        "the in-flight message was not observed in the output stream within %ss "
        "of the replacement starting (observed: %s)"
        % (trial["recovery_deadline"], first_seen)
    )


def test_output_is_exactly_one_record_per_accepted_message(drills):
    """Counting on the output stream itself: every accepted message once, with
    its original payload, and nothing else."""
    for variant in VARIANTS:
        trial = _trial(drills, variant)
        expected = trial["expected"]
        data = [
            record for record in trial["records"] if record.get("kind") == "DATA"
        ]
        seen = {}
        for record in data:
            seen.setdefault(record.get("messageId"), []).append(record)
        problems = []
        for message_id, payload in expected.items():
            found = seen.get(message_id, [])
            if len(found) != 1:
                problems.append(
                    "%r: expected 1 record, found %d" % (message_id, len(found))
                )
            elif found[0].get("payload") != payload:
                problems.append(
                    "%r: payload %r is not the payload it arrived with"
                    % (message_id, found[0].get("payload"))
                )
        for message_id in seen:
            if message_id not in expected:
                problems.append("%r: output record for a message nobody accepted" % message_id)
        assert not problems, "%s trial:\n%s" % (variant, "\n".join("  %s" % p for p in problems))
        assert len(data) == len(expected), (
            "%s trial: %d data records in the output stream, expected %d"
            % (variant, len(data), len(expected))
        )


def test_partition_is_settled_after_the_replacement(drills):
    """The checkpoint reaches the last submitted position, no row is left with
    an undecided publication state, and the replacement quiesced."""
    for variant in VARIANTS:
        trial = _trial(drills, variant)
        target = trial["target_id"]
        assert trial["final_checkpoint"] == str(trial["final_position"]), (
            "%s trial: checkpoint is %r, expected %r"
            % (variant, trial["final_checkpoint"], trial["final_position"])
        )
        undecided = {
            message_id: row
            for message_id, row in trial["rows"].items()
            if row.get("publication_state") not in (None, "CONFIRMED")
        }
        assert not undecided, (
            "%s trial: rows left with an undecided publication state:\n%s"
            % (
                variant,
                "\n".join(
                    "  %s -> %s" % (key, json.dumps(value, default=str))
                    for key, value in undecided.items()
                ),
            )
        )
        assert trial["quiesced"], (
            "%s trial: the replacement never reported QUIESCED\nworker output:\n%s"
            % (
                variant,
                "\n".join("  %s" % json.dumps(e) for e in trial["events"][-20:]) or "  (nothing)",
            )
        )


def test_every_publication_went_through_the_append_service(drills):
    """No output record exists that the append service did not write, and the
    service wrote nothing outside the accepted messages."""
    for variant in VARIANTS:
        trial = _trial(drills, variant)
        expected = trial["expected"]
        in_stream = {
            record.get("sequence")
            for record in trial["records"]
            if record.get("kind") == "DATA"
        }
        service_sequences = set(trial["service_sequences"])
        assert in_stream <= service_sequences, (
            "%s trial: %d output record(s) were not written by the append service"
            % (variant, len(in_stream - service_sequences))
        )
        stray = [mid for mid in trial["appended_ids"] if mid not in expected]
        assert not stray, "%s trial: the service appended %r" % (variant, stray)


def test_recovery_is_bounded(drills):
    """The in-flight message is in the output stream within the recovery bound
    of the replacement starting, in every trial."""
    for variant in VARIANTS:
        trial = _trial(drills, variant)
        first_seen = trial["target_first_seen"]
        assert first_seen is not None, (
            "%s trial: the in-flight message was never observed in the output stream"
            % variant
        )
        assert first_seen <= float(trial["recovery_deadline"]), (
            "%s trial: the in-flight message appeared after %.1fs, the bound is %ss"
            % (variant, first_seen, trial["recovery_deadline"])
        )
