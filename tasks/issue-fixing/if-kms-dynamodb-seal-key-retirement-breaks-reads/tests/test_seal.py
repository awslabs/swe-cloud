from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import base64
import hashlib
import os
import select
import random
import secrets
import threading
import time

from pathlib import Path
import boto3
from cdk_seed.cfn_deploy import deploy

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from proxy import Proxy
from support import (ENDPOINT, LOGS, Processes, Writer, clients, enc, get,
                     independent_value, read, scan)


@pytest.fixture(scope="session", autouse=True)
def environment():
    LOGS.mkdir(parents=True, exist_ok=True)
    try:
        ddb, kms = clients()
        ddb.list_tables()
        kms.list_keys()
    except Exception as error:
        pytest.fail(f"environment: AWS emulator preflight failed: {error}")


def setup_installation(label):
    ddb, kms = clients()
    for table, key in [("seal_entries", "id"), ("seal_meta", "pk")]:
        try:
            ddb.delete_table(TableName=table)
            ddb.get_waiter("table_not_exists").wait(TableName=table)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
    cfn = boto3.client("cloudformation", endpoint_url=ddb.meta.endpoint_url,
                       region_name=ddb.meta.region_name,
                       aws_access_key_id="test", aws_secret_access_key="test")
    outputs = deploy(cfn, "seal-" + label, Path(__file__).parent / "cdk_seed/seed.template.json", Label=label)
    for table in ["seal_entries", "seal_meta"]:
        ddb.get_waiter("table_exists").wait(TableName=table)
    keys = [kms.describe_key(KeyId=outputs[f"Key{number}Id"])["KeyMetadata"]
            for number in range(3)]
    alias = f"alias/seal-{label}"
    ddb.put_item(TableName="seal_meta", Item=enc({"pk": "config", "key_id": alias,
                                                 "config_version": 1}))
    return ddb, kms, keys, alias


@pytest.mark.parametrize("change", ["arn", "alias"])
def test_retirement(change):
    label = f"{change}-{secrets.token_hex(4)}"
    rng = random.Random(label)
    ddb, kms, keys, alias = setup_installation(label)
    processes = Processes(label)
    proxies = [Proxy(ENDPOINT) for _ in range(7)]
    ledger, errors = {}, []
    ledger_lock = threading.Lock()
    stop = threading.Event()
    held_result = []
    late_result = []
    late_thread = None
    threads = []
    first_complete = None
    release_at = None
    m1 = m2 = None
    record = None
    completed_successfully = False
    crash_seen = False
    conflict_done = False
    final = {}

    def issue(error):
        errors.append(str(error))

    try:
        writers = [Writer(processes.start(["writer", "--id", f"site-{i}"],
                                          proxies[i].endpoint, f"writer-{i}"),
                          ledger, ledger_lock) for i in range(3)]
        cohorts = [[f"{label}-{group}-{i:03d}" for i in range(rng.randint(18, 24))]
                   for group in ("east", "west")]
        hot = [[f"{label}-active-{w}-{i}" for i in range(6)] for w in (1, 2)]
        # Preload through the service, keeping an independent ledger of its acknowledgements.
        for identity in cohorts[0]:
            writers[0].put(identity, secrets.token_bytes(rng.randint(0, 96)))
        kms.update_alias(AliasName=alias, TargetKeyId=keys[1]["KeyId"])
        for identity in cohorts[1] + hot[0] + hot[1]:
            writers[0].put(identity, secrets.token_bytes(rng.randint(0, 96)))
        # An entry the release being replaced wrote before this installation was upgraded,
        # in that release's envelope format. Its request is retried on the repaired service.
        legacy_id = f"{label}-legacy"
        legacy_value = secrets.token_bytes(52)
        legacy_request = dict(req=f"{label}-legacy-put", op="put", id=legacy_id,
                              value_b64=base64.b64encode(legacy_value).decode())
        dek, iv = os.urandom(32), os.urandom(12)
        wrapped = kms.encrypt(KeyId=alias, Plaintext=dek)
        ddb.put_item(TableName="seal_entries", Item=enc(dict(
            id=legacy_id, key_arn=wrapped["KeyId"], wrapped_dek=wrapped["CiphertextBlob"], iv=iv,
            ct=AESGCM(dek).encrypt(iv, legacy_value, None), version=1,
            last_req=legacy_request["req"],
            last_digest=hashlib.sha256(legacy_value).hexdigest())))
        writers[1].put(legacy_id, legacy_value, req=legacy_request["req"])
        writers[1].put(legacy_id, b"after-legacy-" + secrets.token_bytes(20))
        before = scan(ddb)
        actual_keys = {independent_value(kms, item)[0] for item in before}
        assert actual_keys and actual_keys <= {keys[0]["Arn"], keys[1]["Arn"]}, actual_keys

        held_id = rng.choice(cohorts[0])
        held_value = secrets.token_bytes(77)
        held_req = f"{label}-reconnect"
        history_request = rng.choice(writers[0].requests).copy()
        # Disconnect one writer on either side of the durable effect. A fresh
        # connection must resolve the same request without losing or replaying it.
        uncertain_id = f"{label}-uncertain"
        uncertain_value = secrets.token_bytes(39)
        uncertain_request = dict(req=f"{label}-no-reply", op="put", id=uncertain_id,
                                 value_b64=base64.b64encode(uncertain_value).decode())
        proxies[4].held_id = uncertain_id
        proxies[4].hold_response = change == "alias"
        uncertain = processes.start(["writer", "--id", "disconnected"],
                                    proxies[4].endpoint, "disconnected")
        uncertain.stdin.write(json.dumps(uncertain_request) + "\n")
        uncertain.stdin.flush()
        until = time.monotonic() + 10
        while time.monotonic() < until:
            pause = proxies[4].held
            if pause and not proxies[4].hold_response:
                break
            if pause and pause.applied.is_set() and 200 <= pause.status < 300:
                item = read(ddb, "seal_entries", {"id": uncertain_id})
                if item and independent_value(kms, item)[1] == uncertain_value:
                    break
                pause.release.set()
                with proxies[4].lock:
                    proxies[4].held = None
            time.sleep(.02)
        else:
            raise AssertionError("writer did not reach the requested network interruption")
        if proxies[4].hold_response:
            assert independent_value(kms, read(ddb, "seal_entries", {"id": uncertain_id}))[1] == uncertain_value
        uncertain.kill()
        uncertain.wait(timeout=5)
        pause.drop = True
        pause.release.set()
        if not proxies[4].hold_response:
            # A request whose persistence was never delivered must not be mistaken
            # for an already committed request by a reconnecting writer.
            assert pause.done.wait(5), "dropped request did not drain"
            writers[1].put(uncertain_id, uncertain_value, req=uncertain_request["req"])
            assert independent_value(kms, read(ddb, "seal_entries", {"id": uncertain_id}))[1] == uncertain_value
        writers[1].put(uncertain_id, b"newer-" + secrets.token_bytes(39))
        proxies[0].held_id = held_id
        proxies[0].known_ids = set(ledger)

        def delayed_put():
            try:
                writers[0].put(held_id, held_value, timeout=100, req=held_req)
                held_result.append((True, time.monotonic()))
            except Exception as error:
                held_result.append((False, time.monotonic()))
                issue(error)

        held_thread = threading.Thread(target=delayed_put, daemon=True)
        held_thread.start()
        threads.append(held_thread)
        until = time.monotonic() + 10
        while proxies[0].held is None and time.monotonic() < until:
            time.sleep(.02)
        assert proxies[0].held is not None, "writer did not issue the entry persistence request"

        # Two more requests the network delays, both at KMS and both until after the
        # operator has retired the former keys: a read of an existing entry and a put.
        late_get_id = rng.choice([identity for identity in cohorts[0] if identity != held_id])
        late_get_before = ledger[late_get_id]
        proxies[5].hold_kms = {"Decrypt"}
        late_get = processes.start(["get", late_get_id], proxies[5].endpoint, "delayed-get")
        proxies[6].hold_kms = {"Encrypt", "GenerateDataKey", "GenerateDataKeyWithoutPlaintext"}
        late_put_id = f"{label}-delayed-put"
        late_writer = Writer(processes.start(["writer", "--id", "delayed"], proxies[6].endpoint,
                                             "delayed-writer"), ledger, ledger_lock)

        def late_put():
            try:
                late_writer.put(late_put_id, secrets.token_bytes(41), timeout=100,
                                req=f"{label}-delayed-put")
                late_result.append((True, time.monotonic()))
            except Exception as error:
                late_result.append((False, time.monotonic()))
                issue(f"put delayed at KMS until after retirement: {error}")

        late_thread = threading.Thread(target=late_put, daemon=True)
        late_thread.start()
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if ((proxies[5].kms_held or late_get.poll() is not None)
                    and (proxies[6].kms_held or late_result)):
                break
            time.sleep(.02)

        if change == "arn":
            ddb.put_item(TableName="seal_meta", Item=enc({"pk": "config",
                         "key_id": keys[2]["Arn"], "config_version": 2}))
        else:
            kms.update_alias(AliasName=alias, TargetKeyId=keys[2]["KeyId"])
            with ThreadPoolExecutor(max_workers=6) as readers:
                futures = {readers.submit(get, identity): identity
                           for identity in cohorts[0] + cohorts[1]}
                for future in as_completed(futures):
                    identity = futures[future]
                    try:
                        assert future.result() == ledger[identity], f"read mismatch before migrate: {identity}"
                    except Exception as error:
                        issue(error)

        def load(index):
            number = 0
            while not stop.is_set():
                started = time.monotonic()
                identity = (hot[index - 1][(number // 2) % len(hot[index - 1])]
                            if number % 2 == 0 else f"{label}-traffic-{index}-{number}")
                value = f"put-{number}-".encode() + secrets.token_bytes(31)
                try:
                    writers[index].put(identity, value)
                except Exception as error:
                    issue(error)
                    return
                number += 1
                stop.wait(max(0, .2 - (time.monotonic() - started)))

        def observe():
            stable = [identity for identity in cohorts[0] + cohorts[1] if identity != held_id]
            number = 0
            while not stop.is_set():
                started = time.monotonic()
                try:
                    for key in keys[:2]:
                        state = kms.describe_key(KeyId=key["KeyId"])["KeyMetadata"]["KeyState"]
                        assert state == "Enabled", f"premature key state {key['KeyId']}: {state}"
                    identity = stable[number % len(stable)]
                    with ledger_lock:
                        previous = ledger[identity]
                    value = get(identity)
                    with ledger_lock:
                        current = ledger[identity]
                    assert value in (previous, current), f"read returned stale value: {identity}"
                    number += 1
                except Exception as error:
                    issue(error)
                stop.wait(max(0, .2 - (time.monotonic() - started)))

        for target, args in [(load, (1,)), (load, (2,)), (observe, ())]:
            thread = threading.Thread(target=target, args=args, daemon=True)
            thread.start()
            threads.append(thread)

        migrator_proxy = proxies[3]
        migrator_proxy.migration = True
        migrator_proxy.known_ids = set(ledger)
        migrator_proxy.excluded_id = held_id
        started = time.monotonic()
        m1 = processes.start(["migrate"], migrator_proxy.endpoint, "migrator-1")
        deadline = started + 45
        while time.monotonic() < deadline:
            now = time.monotonic()
            if migrator_proxy.conflict is not None and not conflict_done:
                pause = migrator_proxy.conflict
                identity = pause.ids[0]
                try:
                    writers[1].put(identity, b"replacement-" + secrets.token_bytes(45))
                except Exception as error:
                    issue(error)
                finally:
                    pause.release.set()
                    conflict_done = True
            if migrator_proxy.crash is not None and not crash_seen:
                m1.kill()
                m1.wait(timeout=5)
                migrator_proxy.crash.drop = True
                migrator_proxy.crash.release.set()
                crash_seen = True
            if m2 is None and m1.poll() is not None:
                # Also rerun commands which finish in fewer than nine write requests.
                migrator_proxy.migration = False
                m2 = processes.start(["migrate"], migrator_proxy.endpoint, "migrator-2")
            record = read(ddb, "seal_meta", {"pk": "migration"})
            if record and record.get("status") == "complete" and first_complete is None:
                first_complete = now
            if first_complete is not None:
                if release_at is None and now >= first_complete + 2:
                    release_at = time.monotonic()
                    proxies[0].held.release.set()
                if now >= first_complete + 3 and m2 is not None and m2.poll() is not None:
                    completed_successfully = m2.returncode == 0 and record.get("status") == "complete"
                    break
            if m2 is not None and m2.poll() is not None and m2.returncode != 0:
                issue(f"replacement migrator exited {m2.returncode}")
                break
            time.sleep(.04)

        if not completed_successfully:
            issue("migration failed to complete successfully within 45 seconds including restart")
        if first_complete is None:
            issue("completion record never appeared")
        if release_at is None:
            release_at = time.monotonic()
            proxies[0].held.release.set()
        stop.set()
        for thread in threads:
            thread.join(timeout=10)
        for thread in threads:
            if thread.is_alive():
                issue("request or observer failed to drain")
        if not held_result or not held_result[0][0]:
            issue("delayed writer did not acknowledge its put")
        elif held_result[0][1] - release_at > 5:
            issue("delayed writer took more than 5 seconds after delivery")
        for writer in writers[1:]:
            late = [(identity, elapsed) for identity, elapsed in writer.durations if elapsed > 5]
            if late:
                issue(f"late acknowledgements: {late}")
            if writer.proc.poll() is not None:
                issue("live writer terminated")
        record = read(ddb, "seal_meta", {"pk": "migration"})
        if not record or record.get("status") != "complete" or record.get("target_arn") != keys[2]["Arn"]:
            issue(f"invalid completion record: {record}")
        elif int(record.get("config_version", -1)) != (2 if change == "arn" else 1):
            issue(f"wrong config version in completion: {record}")

        # The oracle owns retirement. Service reads cannot mask incorrect storage here.
        for key in keys[:2]:
            state = kms.describe_key(KeyId=key["KeyId"])["KeyMetadata"]["KeyState"]
            if state != "Enabled":
                issue(f"old key was not Enabled before retirement: {state}")
            kms.disable_key(KeyId=key["KeyId"])
            until = time.monotonic() + 5
            while time.monotonic() < until:
                if kms.describe_key(KeyId=key["KeyId"])["KeyMetadata"]["KeyState"] == "Disabled":
                    break
                time.sleep(.05)
            else:
                issue("environment: DisableKey did not take effect")
        # The delayed KMS requests are delivered now; each has its budget from here.
        delivered = time.monotonic()
        for proxy in proxies[5:7]:
            proxy.kms_release.set()
        try:
            output, _ = late_get.communicate(timeout=5)
            assert late_get.returncode == 0, f"exit {late_get.returncode}"
            assert base64.b64decode(output.strip(), validate=True) in (
                late_get_before, ledger[late_get_id]), "wrong value"
        except Exception as error:
            issue(f"read delayed at KMS until after retirement: {late_get_id}: {error!r}")
        late_thread.join(timeout=6)
        if not late_result:
            issue("put delayed at KMS until after retirement was not acknowledged within 5 seconds")
        elif late_result[0][0] and late_result[0][1] - delivered > 5:
            issue("put delayed at KMS took more than 5 seconds after delivery")
        retry_writer = Writer(processes.start(["writer", "--id", "reconnected"],
                                               ENDPOINT, "reconnected"), ledger, ledger_lock)
        for request in [history_request, uncertain_request, legacy_request]:
            try:
                retry_writer.put(request["id"], base64.b64decode(request["value_b64"]),
                                 req=request["req"], record=False)
            except Exception as error:
                issue(f"retry failed after retirement: {error}")
        # An ID belongs to its original input, even on another connection.
        for identity, value in [(history_request["id"], b"conflict-" + secrets.token_bytes(11)),
                                (f"{label}-other-id", base64.b64decode(history_request["value_b64"]))]:
            try:
                proc = retry_writer.proc
                conflict = dict(history_request, id=identity, value_b64=base64.b64encode(value).decode())
                proc.stdin.write(json.dumps(conflict) + "\n")
                proc.stdin.flush()
                assert select.select([proc.stdout], [], [], 5)[0], "conflicting retry timed out"
                reply = json.loads(proc.stdout.readline())
                assert reply.get("req") == conflict["req"] and reply.get("ok") is False, reply
            except Exception as error:
                issue(f"request ID reuse was accepted or stalled: {error}")
        unique = {}
        for writer in writers + [late_writer]:
            for request in writer.requests:
                unique[request["req"]] = request["id"]
        unique[uncertain_request["req"]] = uncertain_id
        expected_revisions = {}
        for identity in unique.values():
            expected_revisions[identity] = expected_revisions.get(identity, 0) + 1
        for item in scan(ddb):
            identity = item["id"]
            try:
                key_arn, value = independent_value(kms, item)
                assert key_arn == item["key_arn"] == keys[2]["Arn"], f"wrong wrapping key: {identity}"
                final[identity] = value
                assert int(item["version"]) == expected_revisions.get(identity), (
                    f"application revision does not count unique commits: {identity}: "
                    f"{item['version']} != {expected_revisions.get(identity)}")
            except Exception as error:
                issue(f"unreadable envelope {identity}: {error}")
        for identity, expected in ledger.items():
            if final.get(identity) != expected:
                issue(f"missing or reverted acknowledged value: {identity}")
        if final.get(held_id) != held_value:
            issue("delayed put's value did not survive retirement")
        with (LOGS / f"{label}.json").open("w") as file:
            json.dump({"errors": errors, "entries": len(final), "acked": len(ledger),
                       "crashed": crash_seen, "overlap": conflict_done,
                       "completion": record, "proxy": [p.events for p in proxies],
                       "released_response": proxies[0].held.response,
                       "uncertain_request": uncertain_request,
                       "lost_response_after_commit": proxies[4].hold_response,
                       "expected_revisions": expected_revisions}, file, default=str, indent=2)
        assert not errors, "\n".join(errors)
    finally:
        stop.set()
        for proxy in proxies:
            for pause in [proxy.held, proxy.conflict, proxy.crash, *proxy.kms_held]:
                if pause and not pause.release.is_set():
                    pause.drop = True
                    pause.release.set()
        processes.close()
        for thread in threads + ([late_thread] if late_thread else []):
            thread.join(timeout=6)
        for proxy in proxies:
            proxy.close()
        for key in keys:
            try:
                kms.enable_key(KeyId=key["KeyId"])
            except Exception:
                pass
