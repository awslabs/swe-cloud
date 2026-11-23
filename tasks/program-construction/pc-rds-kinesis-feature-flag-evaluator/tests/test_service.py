"""The server process: command-line options, listener configuration, generic
routes, shutdown and persistence across restarts."""

import subprocess
import time
import urllib.error
import urllib.request

from flagr_support import free_port, assert_doc, assert_rows
from harness import run_program, start_command, workspace


def test_help_and_unknown_option():
    r = run_program(["--help"], timeout=20)
    assert r.returncode == 0, r
    text = r.stdout + r.stderr
    assert "--host=" in text and "--port=" in text, text
    bad = run_program(["--no-such-option"], timeout=20)
    assert bad.returncode == 1, bad


def test_health_and_generic_routes(flagr):
    s = flagr(recorder=False)
    r = s.call("GET", "/health")
    assert r.status == 200
    assert_doc({k: v for k, v in r.json.items() if k != "evalOnlyMode"}, {"status": "OK"})
    assert r.json.get("evalOnlyMode", False) is False
    assert s.call("POST", "/health").status == 405
    assert s.call("DELETE", "/flags").status == 405
    r = s.call("GET", "/no/such/route")
    assert r.status == 404 and r.json.get("message"), r


def test_port_option_overrides_environment(flagr):
    s = flagr(recorder=False)
    s.stop()
    option_port, env_port = free_port(), free_port()
    env = dict(s.environment(), PORT=str(env_port))
    with workspace() as cwd, open(s.log_path, "ab") as log:
        process = start_command(["/app/program", f"--port={option_port}", "--host=127.0.0.1"], cwd=cwd, env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline, body = time.time() + 40, None
            while time.time() < deadline and body is None:
                assert process.poll() is None, s.log_tail()
                try:
                    body = urllib.request.urlopen(f"http://127.0.0.1:{option_port}/api/v1/health", timeout=2).read()
                except OSError:
                    time.sleep(0.2)
            assert body is not None, s.log_tail()
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{env_port}/api/v1/health", timeout=2)
                raise AssertionError("the server also listens on the PORT environment value")
            except urllib.error.URLError:
                pass
        finally:
            process.terminate()
            try:
                process.wait(15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def test_state_survives_restart(flagr):
    s = flagr(recorder=False)
    f = s.flag("persist", variants=[("on", {"k": [1, "two"]}), "off"])
    s.segment(f, "adults", 100, [("on", 100)], [("age", "GTE", "18")])
    s.ok("PUT", "/flags/1/enabled", {"enabled": True})
    s.ok("POST", "/flags/1/tags", {"value": "keep"})
    s.sync()
    before = s.ok("GET", "/flags/1")
    first = s.evaluate({"entityID": "u1", "flagKey": "persist", "entityContext": {"age": 30}})
    assert first.get("variantKey") == "on", first
    # SIGTERM stops the server cleanly.
    assert s.stop() == 0
    s.launch()
    assert_doc(s.ok("GET", "/flags/1"), before)
    # Evaluation state is loaded from storage at start-up.
    deadline = time.time() + 10
    again = s.evaluate({"entityID": "u1", "flagKey": "persist", "entityContext": {"age": 30}})
    while again.get("variantKey") != "on" and time.time() < deadline:
        time.sleep(0.1)
        again = s.evaluate({"entityID": "u1", "flagKey": "persist", "entityContext": {"age": 30}})
    assert_doc(again, first)
    # Identifiers continue after the stored ones.
    assert s.ok("POST", "/flags", {"description": "after", "key": "after"})["id"] == 3
    assert_doc(s.ok("GET", "/flags/snapshots/max_id"), {"maxID": 10})
    assert_rows(s.rows("SELECT id, key FROM flags ORDER BY id"), [(1, "persist"), (2, "zz-sync-1"), (3, "after")])
