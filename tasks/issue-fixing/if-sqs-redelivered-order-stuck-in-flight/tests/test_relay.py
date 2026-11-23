"""Verifier for fulfilment-relay: one live run against SQS through the gateway, graded on SQS state
and on what the gateway and the fulfilment service observed."""

import json
from pathlib import Path
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import boto3
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdk_seed.cfn_deploy import deploy
from harness import DELETE_RESPONSE_SECONDS, EXTENSION_RESPONSE_SECONDS, Fulfilment, Gateway  # noqa: E402

UPSTREAM = os.environ.get("VERIFIER_SQS_UPSTREAM", "http://localhost:4566")
APP = os.environ.get("VERIFIER_APP_DIR", "/app")
LOG_DIR = os.environ.get("VERIFIER_LOG_DIR", "/logs/verifier")

VISIBILITY_TIMEOUT = 6
MAX_RECEIVE_COUNT = 5
REDELIVER_VISIBILITY_TIMEOUT = 0
CONCURRENCY = 4
HOLD = random.uniform(54.0, 58.0)
SECOND_BLACKOUT = 8.0
READY_TIMEOUT = 20.0
DUPLICATE_DEADLINE = 14.0
# Room for the release itself: a heartbeat call still out when the fault comes back (its answer
# can take EXTENSION_RESPONSE_SECONDS) plus the next receive.
RETRY_SLACK = 7.0
SETTLE_DEADLINE = 20.0
QUIET_WINDOW = 15.0
PLAIN_DEADLINE = 10.0
# Second, short run: a non-zero redeliver delay, with a redelivery in hand when the fault returns.
DELAYED_REDELIVER_VISIBILITY_TIMEOUT = 5
DELAYED_HOLD = 15.0
# Visibility timing on the emulator can be up to a second coarse.
DELAY_EARLY_SLACK = 2.0


def sqs_client():
    return boto3.client("sqs", endpoint_url=UPSTREAM, region_name="us-east-1",
                        aws_access_key_id="test", aws_secret_access_key="test")


def depth(sqs, url):
    attributes = sqs.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
                        "ApproximateNumberOfMessagesDelayed"],
    )["Attributes"]
    return {k: int(v) for k, v in attributes.items()}


def wait_until(predicate, timeout, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def dump(name, data):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, name), "w") as fh:
            json.dump(data, fh, indent=1, default=str)
    except OSError:
        pass


def make_queues(sqs):
    run_id = uuid.uuid4().hex[:10]
    cfn = boto3.client("cloudformation", endpoint_url=sqs.meta.endpoint_url,
                       region_name=sqs.meta.region_name,
                       aws_access_key_id="test", aws_secret_access_key="test")
    outputs = deploy(cfn, "seed-orders-" + run_id,
                     Path(__file__).parent / "cdk_seed/seed.template.json",
                     QueueName=f"orders-{run_id}", DlqName=f"orders-dlq-{run_id}",
                     VisibilityTimeout=VISIBILITY_TIMEOUT, MaxReceiveCount=MAX_RECEIVE_COUNT)
    queue_url, dlq_url, dlq_arn = outputs["QueueUrl"], outputs["DlqUrl"], outputs["DlqArn"]
    assert sum(depth(sqs, queue_url).values()) == 0, "fresh orders queue is not empty"
    return queue_url, dlq_url, dlq_arn


def new_key():
    return f"ord-{uuid.uuid4().hex[:8]}"


def start_relay(gateway, fulfilment, queue_url, redeliver_visibility_timeout, workdir):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VERIFIER_")}
    env.update({
        "SQS_ENDPOINT_URL": gateway.url,
        "AWS_ENDPOINT_URL": gateway.url,
        "AWS_ENDPOINT_URL_SQS": gateway.url,
        "QUEUE_URL": queue_url,
        "WORK_URL": fulfilment.url,
        "CONCURRENCY": str(CONCURRENCY),
        "REDELIVER_VISIBILITY_TIMEOUT_SECONDS": str(redeliver_visibility_timeout),
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "PYTHONPATH": APP,
        "PYTHONUNBUFFERED": "1",
    })
    entry = os.path.join(APP, "run-consumer")
    command = [entry] if os.access(entry, os.X_OK) else ["python3", entry]
    relay_log = open(os.path.join(workdir, "relay.log"), "wb")
    proc = subprocess.Popen(command, cwd=workdir, env=env, stdout=relay_log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    return proc, relay_log


def stop_relay(proc, relay_log, workdir, obs):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    relay_log.close()
    try:
        with open(os.path.join(workdir, "relay.log"), "rb") as fh:
            obs["relay_log_tail"] = fh.read()[-20000:].decode(errors="replace")
    except OSError:
        pass


@pytest.fixture(scope="module")
def run():
    sqs = sqs_client()
    sqs.list_queues()  # preflight: the emulator answers
    queue_url, dlq_url, dlq_arn = make_queues(sqs)

    keys = {"fail_then_ok": new_key(), "ok": new_key(), "poison": new_key(),
            "plain1": new_key(), "plain2": new_key()}
    gateway = Gateway(UPSTREAM).start()
    gateway.extension_latency = random.uniform(*EXTENSION_RESPONSE_SECONDS)
    gateway.delete_latency = DELETE_RESPONSE_SECONDS
    fulfilment = Fulfilment(hold=HOLD).start()
    fulfilment.roles = {keys["fail_then_ok"]: "hold_fault", keys["ok"]: "hold_ok", keys["poison"]: "poison"}
    gateway.black_out(keys["fail_then_ok"], 1, HOLD)
    gateway.black_out(keys["fail_then_ok"], 2, SECOND_BLACKOUT)
    gateway.black_out(keys["ok"], 1, HOLD)

    workdir = tempfile.mkdtemp(prefix="relay-")
    proc, relay_log = start_relay(gateway, fulfilment, queue_url, REDELIVER_VISIBILITY_TIMEOUT, workdir)
    obs = {"keys": keys, "queue_url": queue_url, "dlq_url": dlq_url, "dlq_arn": dlq_arn, "hold": HOLD,
           "extension_latency": gateway.extension_latency, "delete_latency": gateway.delete_latency,
           "problems": []}
    try:
        ready = wait_until(gateway.first_receive_time, READY_TIMEOUT)
        if not ready:
            obs["problems"].append(f"relay made no ReceiveMessage call through SQS_ENDPOINT_URL within {READY_TIMEOUT}s")
            return obs

        message_ids = {}
        first = ["fail_then_ok", "ok", "poison"]
        random.shuffle(first)
        t_send = time.time()
        for role in first:
            body = json.dumps({"order_id": keys[role], "sku": f"SKU-{random.randint(100, 999)}"})
            message_ids[role] = sqs.send_message(QueueUrl=queue_url, MessageBody=body)["MessageId"]
        obs["t_send"] = t_send
        obs["message_ids"] = message_ids

        time.sleep(max(0.0, t_send + 12 - time.time()))
        obs["plain_sent"] = {}
        for role in ("plain1", "plain2"):
            body = json.dumps({"order_id": keys[role], "sku": "SKU-1"})
            message_ids[role] = sqs.send_message(QueueUrl=queue_url, MessageBody=body)["MessageId"]
            obs["plain_sent"][role] = time.time()

        def held_calls_done():
            calls = [fulfilment.calls_for(keys["fail_then_ok"]), fulfilment.calls_for(keys["ok"])]
            if all(c and c[0]["end"] for c in calls):
                return max(c[0]["end"] for c in calls)
            return None

        hold_end = wait_until(held_calls_done, HOLD + 30)
        obs["hold_end"] = hold_end
        if not hold_end:
            obs["problems"].append("the two slow fulfilment calls were never made or never finished")
            hold_end = time.time()

        def drained():
            d = depth(sqs, queue_url)
            return time.time() if sum(d.values()) == 0 else None

        drained_at = wait_until(drained, max(0.0, hold_end + SETTLE_DEADLINE - time.time()), interval=0.5)
        obs["drained_at"] = drained_at
        obs["depth_after_settle"] = depth(sqs, queue_url)
        if drained_at:
            time.sleep(max(0.0, drained_at + QUIET_WINDOW - time.time()))
            obs["depth_after_quiet"] = wait_until(
                lambda: (lambda d: d if sum(d.values()) == 0 else None)(depth(sqs, queue_url)), 2.0
            ) or depth(sqs, queue_url)
            obs["calls_in_quiet_window"] = [c for c in fulfilment.all_calls() if c["start"] > drained_at + 1.0]

        dlq = []
        empty = 0
        while empty < 2:
            got = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                                      VisibilityTimeout=120, AttributeNames=["All"]).get("Messages", [])
            empty = empty + 1 if not got else 0
            dlq.extend(got)
        obs["dlq"] = [{"message_id": m["MessageId"], "key": json.loads(m["Body"]).get("order_id")} for m in dlq]
        obs["queue_attributes"] = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"])["Attributes"]
        obs["alive"] = proc.poll() is None
        obs["calls"] = fulfilment.all_calls()
        obs["deliveries"] = {role: gateway.deliveries_of(mid) for role, mid in message_ids.items()}
        obs["settles"] = {role: gateway.settles(mid) for role, mid in message_ids.items()}
        return obs
    finally:
        stop_relay(proc, relay_log, workdir, obs)
        with gateway.lock:
            obs["gateway_events"] = list(gateway.events)
        dump("observations.json", obs)
        gateway.stop()
        fulfilment.stop()


@pytest.fixture(scope="module")
def delayed_run():
    """One order whose slow first attempt fails after SQS redelivered it, with a non-zero
    REDELIVER_VISIBILITY_TIMEOUT_SECONDS."""
    sqs = sqs_client()
    queue_url, _, _ = make_queues(sqs)
    key = new_key()
    gateway = Gateway(UPSTREAM).start()
    gateway.extension_latency = random.uniform(*EXTENSION_RESPONSE_SECONDS)
    gateway.delete_latency = DELETE_RESPONSE_SECONDS
    fulfilment = Fulfilment(hold=DELAYED_HOLD).start()
    fulfilment.roles = {key: "hold_fault"}
    gateway.black_out(key, 1, DELAYED_HOLD)

    workdir = tempfile.mkdtemp(prefix="relay-delayed-")
    proc, relay_log = start_relay(gateway, fulfilment, queue_url, DELAYED_REDELIVER_VISIBILITY_TIMEOUT, workdir)
    obs = {"key": key, "queue_url": queue_url, "problems": []}
    try:
        if not wait_until(gateway.first_receive_time, READY_TIMEOUT):
            obs["problems"].append(f"relay made no ReceiveMessage call through SQS_ENDPOINT_URL within {READY_TIMEOUT}s")
            return obs
        body = json.dumps({"order_id": key, "sku": "SKU-2"})
        obs["message_id"] = sqs.send_message(QueueUrl=queue_url, MessageBody=body)["MessageId"]

        def retried():
            got = fulfilment.calls_for(key)
            return len(got) >= 2 and got[1]["end"] is not None

        wait_until(retried, DELAYED_HOLD + DELAYED_REDELIVER_VISIBILITY_TIMEOUT + RETRY_SLACK + 15)
        time.sleep(2.0)  # let a stray extra call show up
        obs["calls"] = fulfilment.calls_for(key)
        obs["deliveries"] = gateway.deliveries_of(obs["message_id"])
        return obs
    finally:
        stop_relay(proc, relay_log, workdir, obs)
        with gateway.lock:
            obs["gateway_events"] = list(gateway.events)
        dump("observations-delayed.json", obs)
        gateway.stop()
        fulfilment.stop()


def scenario_ran(run):
    assert not run["problems"], "; ".join(run["problems"])


def calls(run, role):
    return [c for c in run["calls"] if c["key"] == run["keys"][role]]


def test_redelivery_happened_while_orders_were_in_progress(run):
    scenario_ran(run)
    for role in ("fail_then_ok", "ok"):
        deliveries = run["deliveries"][role]
        assert len(deliveries) >= 2, (
            f"order {role}: SQS redelivered it after its heartbeat was lost, but the relay never received "
            f"that redelivery (deliveries seen: {len(deliveries)})")
        gap = deliveries[1]["t"] - deliveries[0]["t"]
        assert gap <= DUPLICATE_DEADLINE, f"order {role}: redelivery received {gap:.1f}s after the first delivery"


def test_orders_queue_drains_and_stays_drained(run):
    scenario_ran(run)
    assert run["drained_at"], (
        f"orders queue still not empty {SETTLE_DEADLINE:.0f}s after the slow calls returned: "
        f"{run['depth_after_settle']}")
    assert sum(run["depth_after_quiet"].values()) == 0, (
        f"a message reappeared in the orders queue {QUIET_WINDOW:.0f}s after it had drained: {run['depth_after_quiet']}")
    assert not run["calls_in_quiet_window"], f"fulfilment calls after the queue drained: {run['calls_in_quiet_window']}"


def test_succeeded_order_is_fulfilled_exactly_once(run):
    scenario_ran(run)
    got = calls(run, "ok")
    assert [c["outcome"] for c in got] == ["success"], f"order that succeeded: fulfilment calls {got}"


def test_failed_order_is_retried_after_the_fault(run):
    scenario_ran(run)
    got = calls(run, "fail_then_ok")
    assert [c["outcome"] for c in got] == ["fault", "success"], (
        f"order whose first attempt failed: expected one failed and one successful fulfilment call, got {got}")
    first, second = got
    assert second["start"] >= first["end"], "retry started before the failed attempt had returned"
    delay = second["start"] - first["end"]
    assert delay <= REDELIVER_VISIBILITY_TIMEOUT + RETRY_SLACK, (
        f"retry started {delay:.1f}s after the fault; REDELIVER_VISIBILITY_TIMEOUT_SECONDS is "
        f"{REDELIVER_VISIBILITY_TIMEOUT}")
    assert second["message_id"] == run["message_ids"]["fail_then_ok"], (
        "the retry was not a redelivery of the original SQS message")


def test_failed_order_waits_out_a_nonzero_redeliver_delay(delayed_run):
    assert not delayed_run["problems"], "; ".join(delayed_run["problems"])
    got = delayed_run["calls"]
    assert [c["outcome"] for c in got] == ["fault", "success"], (
        f"order whose first attempt failed: expected one failed and one successful fulfilment call, got {got}")
    first, second = got
    delay = second["start"] - first["end"]
    redeliver = DELAYED_REDELIVER_VISIBILITY_TIMEOUT
    assert delay >= redeliver - DELAY_EARLY_SLACK, (
        f"retry started {delay:.1f}s after the fault; REDELIVER_VISIBILITY_TIMEOUT_SECONDS is {redeliver}")
    assert delay <= redeliver + RETRY_SLACK, (
        f"retry started {delay:.1f}s after the fault; REDELIVER_VISIBILITY_TIMEOUT_SECONDS is {redeliver}")
    assert second["message_id"] == delayed_run["message_id"], (
        "the retry was not a redelivery of the original SQS message")


def test_no_order_is_handled_twice_at_once(run):
    scenario_ran(run)
    overlapping = [c for c in run["calls"] if c["overlapped"]]
    assert not overlapping, f"fulfilment calls that started while another call for the same order was open: {overlapping}"


def test_dead_letter_queue_holds_only_the_poison_order(run):
    scenario_ran(run)
    keys = sorted(m["key"] or "?" for m in run["dlq"])
    assert keys == [run["keys"]["poison"]], (
        f"dead-letter queue should hold exactly the always-failing order {run['keys']['poison']}; holds {keys} "
        f"(roles: {run['keys']})")


def test_poison_order_is_attempted_on_every_receive(run):
    scenario_ran(run)
    got = calls(run, "poison")
    deliveries = run["deliveries"]["poison"]
    assert got and all(c["outcome"] == "fault" for c in got)
    assert len(got) == len(deliveries), (
        f"always-failing order was received {len(deliveries)} times but fulfilment was attempted {len(got)} times")


def test_other_orders_keep_flowing(run):
    scenario_ran(run)
    for role in ("plain1", "plain2"):
        got = calls(run, role)
        assert [c["outcome"] for c in got] == ["success"], f"order {role}: fulfilment calls {got}"
        waited = got[0]["start"] - run["plain_sent"][role]
        assert waited <= PLAIN_DEADLINE, f"order {role} waited {waited:.1f}s for a handler while others were slow"


def test_relay_stays_up_and_answers_every_call(run):
    scenario_ran(run)
    assert run["alive"], "the relay process exited during the run"
    abandoned = [c for c in run["calls"] if c["abandoned"] or c["end"] is None]
    assert not abandoned, f"fulfilment calls the relay hung up on: {abandoned}"


def test_queue_configuration_untouched(run):
    scenario_ran(run)
    attributes = run["queue_attributes"]
    assert attributes.get("VisibilityTimeout") == str(VISIBILITY_TIMEOUT)
    assert "FifoQueue" not in attributes or attributes["FifoQueue"] == "false"
    redrive = json.loads(attributes.get("RedrivePolicy", "{}"))
    assert redrive.get("deadLetterTargetArn") == run["dlq_arn"]
    assert int(redrive.get("maxReceiveCount", 0)) == MAX_RECEIVE_COUNT
