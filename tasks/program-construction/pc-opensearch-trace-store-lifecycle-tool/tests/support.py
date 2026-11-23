"""Shared helpers: OpenSearch inspection, the trace server, OTLP fixtures and comparison canonicalizers."""

import base64
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

from harness import run_program, start_command, workspace, write_fixture

FAMILIES = ("span", "service", "dependencies")
EXPECTED = json.loads((Path(__file__).parent / "expected" / "templates.json").read_text())


# ---------------------------------------------------------------- OpenSearch REST
class Cluster:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as err:
            raw = err.read().decode()
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, raw

    def get(self, path):
        status, body = self.request("GET", path)
        assert status == 200, (path, status, body)
        return body

    def refresh(self, prefix):
        self.request("POST", f"/{prefix}jaeger-*/_refresh?expand_wildcards=all&ignore_unavailable=true&allow_no_indices=true")

    def indices(self, prefix):
        """Concrete index name -> sorted alias names, for every index named <prefix>jaeger-*."""
        status, body = self.request("GET", f"/{prefix}jaeger-*?expand_wildcards=all&allow_no_indices=true&filter_path=*.aliases")
        assert status == 200, body
        return {name: sorted((info or {}).get("aliases", {})) for name, info in (body or {}).items()}

    def alias_details(self, prefix):
        """Index -> alias -> alias body (e.g. is_write_index flags) exactly as stored."""
        status, body = self.request("GET", f"/{prefix}jaeger-*?expand_wildcards=all&allow_no_indices=true&filter_path=*.aliases")
        return {name: (info or {}).get("aliases", {}) for name, info in (body or {}).items()}

    def settings(self, index):
        return self.get(f"/{index}/_settings?flat_settings=true")[index]["settings"]

    def mappings(self, index):
        return self.get(f"/{index}/_mapping")[index]["mappings"]

    def templates(self, prefix):
        status, body = self.request("GET", f"/_template/{prefix}jaeger-*")
        return body if status == 200 else {}

    def composable_templates(self, prefix):
        status, body = self.request("GET", f"/_index_template/{prefix}jaeger-*")
        return [t["name"] for t in body.get("index_templates", [])] if status == 200 else []

    def search(self, target, query=None, size=200):
        body = {"size": size, "query": query or {"match_all": {}}}
        status, result = self.request("POST", f"/{target}/_search?expand_wildcards=all&ignore_unavailable=true&allow_no_indices=true", body)
        assert status == 200, result
        return result["hits"]["hits"]

    def cleanup(self, base):
        self.request("DELETE", f"/{base}*?expand_wildcards=all")
        self.request("DELETE", f"/_template/{base}*")


def unique_prefix(letter="t"):
    return letter + uuid.uuid4().hex[:9]


@contextmanager
def aging_proxy(cluster, dates):
    """HTTP pass-through reporting creation dates from an index-name -> milliseconds map.

    Every request is forwarded unchanged; only named indices' creation_date settings in
    index metadata/settings and cluster metadata responses are changed.
    """
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def forward(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "accept-encoding", "content-length", "connection", "transfer-encoding")}
            req = urllib.request.Request(cluster.url + self.path, data=body, method=self.command, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    status, rheaders, data = resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as err:
                status, rheaders, data = err.code, err.headers, err.read()
            try:
                response = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                response = None
            changed = False
            if isinstance(response, dict):
                containers = [response]
                metadata = response.get("metadata")
                if isinstance(metadata, dict) and isinstance(metadata.get("indices"), dict):
                    containers.append(metadata["indices"])
                for indices in containers:
                    for index, fake in dates.items():
                        info = indices.get(index)
                        settings = info.get("settings") if isinstance(info, dict) else None
                        if not isinstance(settings, dict):
                            continue
                        if "index.creation_date" in settings:
                            settings["index.creation_date"] = str(fake)
                            changed = True
                        nested = settings.get("index")
                        if isinstance(nested, dict) and "creation_date" in nested:
                            nested["creation_date"] = str(fake)
                            changed = True
            if changed:
                data = json.dumps(response).encode()
            self.send_response(status)
            for k, v in rheaders.items():
                if k.lower() not in ("content-length", "connection", "transfer-encoding", "content-encoding"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = forward

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- es-rollover
def rollover_tool(action, url, *args, env=None, timeout=60):
    return run_program(["es-rollover", action, url, *args], env=env, timeout=timeout)


def ok(result):
    assert result.returncode == 0, f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def failed(result):
    assert result.returncode != 0, f"expected failure\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def family_layout(prefix, generations, read=None, families=FAMILIES):
    """Expected index->aliases map: write alias on the newest generation, read alias on `read` generations."""
    layout = {}
    for family in families:
        gens = generations[family] if isinstance(generations, dict) else generations
        reads = (read or {}).get(family, gens) if isinstance(read, dict) else (read if read is not None else gens)
        for gen in gens:
            aliases = []
            if gen in reads:
                aliases.append(f"{prefix}jaeger-{family}-read")
            if gen == max(gens):
                aliases.append(f"{prefix}jaeger-{family}-write")
            layout[f"{prefix}jaeger-{family}-{gen:06d}"] = sorted(aliases)
    return layout


def expected_template(kind, prefix, shards=1, replicas=0):
    body = json.loads(json.dumps(EXPECTED[kind]).replace("{prefix}", prefix))
    body["settings"]["index"]["number_of_shards"] = str(shards)
    body["settings"]["index"]["number_of_replicas"] = str(replicas)
    return body


def lenient(value):
    """Template bodies as OpenSearch interprets them: boolean/number leaves may be spelled as strings."""
    if isinstance(value, dict):
        return {k: lenient(v) for k, v in value.items()}
    if isinstance(value, list):
        return [lenient(v) for v in value]
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    return value


def assert_template(actual, expected):
    assert set(actual) == {"aliases", "index_patterns", "mappings", "order", "settings"}, sorted(actual)
    assert actual["index_patterns"] == expected["index_patterns"]
    assert actual["order"] == expected["order"] and type(actual["order"]) is int
    assert actual["aliases"] == {}
    assert actual["settings"] == expected["settings"]
    assert lenient(actual["mappings"]) == lenient(expected["mappings"])


# ---------------------------------------------------------------- trace server
def free_ports(count):
    socks = [socket.socket() for _ in range(count)]
    try:
        for sock in socks:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in socks]
    finally:
        for sock in socks:
            sock.close()


CONFIG = """service:
  extensions: [jaeger_storage, jaeger_query, healthcheckv2]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [jaeger_storage_exporter]
  telemetry:
    metrics:
      level: none
extensions:
  healthcheckv2:
    use_v2: true
    http:
      endpoint: 127.0.0.1:{health}
  jaeger_query:
    storage:
      traces: some_storage
    http:
      endpoint: 127.0.0.1:{query}
    grpc:
      endpoint: 127.0.0.1:{query_grpc}
  jaeger_storage:
    backends:
      some_storage:
        opensearch:
          server_urls: ["{url}"]
          indices:
            index_prefix: "{prefix}"
            spans:        {{shards: {shards}, replicas: {replicas}, rotation: {{manual_rollover: {{}}}}}}
            services:     {{shards: {shards}, replicas: {replicas}, rotation: {{manual_rollover: {{}}}}}}
            dependencies: {{shards: {shards}, replicas: {replicas}, rotation: {{manual_rollover: {{}}}}}}
            sampling:     {{shards: {shards}, replicas: {replicas}, rotation: {{manual_rollover: {{}}}}}}
receivers:
  otlp:
    protocols:
      grpc: {{endpoint: 127.0.0.1:{otlp_grpc}}}
      http: {{endpoint: 127.0.0.1:{otlp_http}}}
processors:
  batch:
exporters:
  jaeger_storage_exporter:
    trace_storage: some_storage
"""


class TraceServer:
    def __init__(self, query, otlp):
        self.query = query
        self.otlp = otlp

    def get(self, path, params=None):
        url = self.query + path + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as err:
            raw = err.read().decode()
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, raw

    def post_traces(self, payload):
        req = urllib.request.Request(self.otlp + "/v1/traces", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()

    def post_raw(self, data, content_type="application/json"):
        req = urllib.request.Request(self.otlp + "/v1/traces", data=data, method="POST", headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()

    def ingest(self, payload):
        status, body = self.post_traces(payload)
        assert status == 200, (status, body)


@contextmanager
def trace_server(url, prefix, shards=1, replicas=0):
    """Run the candidate's trace server with a manual-rollover configuration; always terminate it."""
    ports = dict(zip(("health", "query", "query_grpc", "otlp_grpc", "otlp_http"), free_ports(5)))
    with workspace() as root:
        config = root / "config.yaml"
        write_fixture(config, CONFIG.format(url=url, prefix=prefix, shards=shards, replicas=replicas, **ports), mode=0o644)
        log_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"server-{uuid.uuid4().hex[:8]}.log"
        with open(log_path, "wb") as log:
            proc = start_command(["/app/program", "--config", f"file:{config}"], cwd=root, stdout=log, stderr=subprocess.STDOUT)
            server = TraceServer(f"http://127.0.0.1:{ports['query']}", f"http://127.0.0.1:{ports['otlp_http']}")
            try:
                deadline = time.time() + 30
                while True:
                    assert proc.poll() is None, "server exited early:\n" + log_path.read_text(errors="replace")[-3000:]
                    try:
                        if server.get("/api/v3/services")[0] == 200:
                            break
                    except OSError:
                        pass
                    assert time.time() < deadline, "query API did not come up:\n" + log_path.read_text(errors="replace")[-3000:]
                    time.sleep(0.2)
                yield server
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        log_path.unlink(missing_ok=True)


def wait_until(probe, timeout=25, interval=0.3):
    """Poll probe() until it returns a truthy value; return the last value."""
    deadline = time.time() + timeout
    while True:
        value = probe()
        if value or time.time() > deadline:
            return value
        time.sleep(interval)


def wait_docs(cluster, prefix, target, count, timeout=25):
    def probe():
        cluster.refresh(prefix)
        hits = cluster.search(target)
        return hits if len(hits) >= count else None
    hits = wait_until(probe, timeout)
    assert hits, f"{target} did not reach {count} documents"
    return hits


# ---------------------------------------------------------------- OTLP fixtures
BASE = 1485449191000000000  # 2017-01-26T16:46:31Z in Unix nanoseconds
DAY = {"query.start_time_min": "2017-01-26T00:00:00Z", "query.start_time_max": "2017-01-27T00:00:00Z"}


def value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, bytes):
        return {"bytesValue": base64.b64encode(v).decode()}
    if isinstance(v, list):
        return {"arrayValue": {"values": [value(x) for x in v]}}
    if isinstance(v, dict):
        return {"kvlistValue": {"values": [attr(k, x) for k, x in v.items()]}}
    return {"stringValue": v}


def attr(key, v):
    return {"key": key, "value": value(v)}


def otlp_span(trace_id, span_id, name, start, duration, parent=None, kind=None, attrs=None, **extra):
    span = {"traceId": trace_id, "spanId": span_id, "name": name,
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + duration)}
    if kind is not None:
        span["kind"] = kind
    if parent:
        span["parentSpanId"] = parent
    if attrs:
        span["attributes"] = [attr(k, v) for k, v in attrs.items()]
    span.update(extra)
    return span


def resource_spans(service, spans, resource_attrs=None, scope=None):
    attrs = ([attr("service.name", service)] if service is not None else []) + [attr(k, v) for k, v in (resource_attrs or {}).items()]
    return {"resource": {"attributes": attrs}, "scopeSpans": [{"scope": scope or {}, "spans": spans}]}


def payload(*groups):
    return {"resourceSpans": list(groups)}


# ---------------------------------------------------------------- canonical forms
def _number(v):
    assert type(v) in (int, float), f"expected a JSON number, got {v!r}"
    return v


def _int64(v):
    """OTLP JSON int64 fields: decimal string or JSON integer (booleans excluded)."""
    if isinstance(v, str):
        return int(v)
    assert type(v) in (int, float) and float(v).is_integer(), f"expected an integer, got {v!r}"
    return int(v)


def any_value(v):
    assert isinstance(v, dict) and len(v) == 1, v
    (kind, inner), = v.items()
    if kind == "stringValue":
        assert isinstance(inner, str)
        return ("string", inner)
    if kind == "boolValue":
        assert type(inner) is bool, inner
        return ("bool", inner)
    if kind == "intValue":
        return ("int", _int64(inner))
    if kind == "doubleValue":
        return ("double", float(_number(inner)))
    if kind == "bytesValue":
        return ("bytes", base64.b64decode(inner))
    if kind == "arrayValue":
        return ("array", tuple(any_value(x) for x in (inner or {}).get("values") or []))
    if kind == "kvlistValue":
        return ("kvlist", tuple(otlp_attrs((inner or {}).get("values"))))
    raise AssertionError(f"unexpected AnyValue {v!r}")


def otlp_attrs(items):
    return sorted(((a["key"], any_value(a["value"])) for a in (items or [])), key=repr)


def otlp_flat(result):
    """Flatten an OTLP trace payload into canonical spans (resource and scope attached); order-free."""
    spans = []
    assert set(result) <= {"resourceSpans"}, sorted(result)
    for rs in result.get("resourceSpans") or []:
        resource = otlp_attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans") or []:
            scope = ss.get("scope") or {}
            for s in ss.get("spans") or []:
                status = s.get("status") or {}
                spans.append({
                    "resource": resource,
                    "scope": (scope.get("name", ""), scope.get("version", "")),
                    "traceId": s["traceId"], "spanId": s["spanId"], "parentSpanId": s.get("parentSpanId", ""),
                    "name": s.get("name", ""), "kind": s.get("kind", 0),
                    "start": _int64(s["startTimeUnixNano"]), "end": _int64(s["endTimeUnixNano"]),
                    "traceState": s.get("traceState", ""), "flags": _int64(s.get("flags", 0)),
                    "attributes": otlp_attrs(s.get("attributes")),
                    "events": sorted(((_int64(e["timeUnixNano"]), e.get("name", ""), otlp_attrs(e.get("attributes")))
                                      for e in s.get("events") or []), key=repr),
                    "links": sorted(((l["traceId"], l["spanId"], l.get("traceState", ""), otlp_attrs(l.get("attributes")))
                                     for l in s.get("links") or []), key=repr),
                    "status": (status.get("code", 0), status.get("message", "")),
                })
    for span in spans:
        assert type(span["kind"]) is int and type(span["status"][0]) is int, span
    return sorted(spans, key=lambda s: (s["traceId"], s["spanId"]))


def tag_value(kv):
    """Typed key/value of a stored or legacy tag; numeric values compare by value, booleans stay booleans."""
    assert set(kv) == {"key", "type", "value"}, kv
    t, v = kv["type"], kv["value"]
    if t == "bool":
        assert type(v) is bool, kv
    elif t in ("int64", "float64"):
        v = _number(v)
    else:
        assert isinstance(v, str), kv
    return (kv["key"], t, v)


def tag_set(items):
    return sorted((tag_value(kv) for kv in items or []), key=repr)


def stored_doc(src):
    """Canonical span document: absent/null/empty lists, maps, parentSpanID and flags are equivalent."""
    known = {"traceID", "spanID", "parentSpanID", "flags", "operationName", "references", "startTime",
             "startTimeMillis", "duration", "tags", "tag", "logs", "process"}
    assert set(src) <= known, sorted(set(src) - known)
    process = src["process"]
    assert set(process) <= {"serviceName", "tags", "tag"}, sorted(process)
    for ref in src.get("references") or []:
        assert set(ref) == {"refType", "traceID", "spanID"}, ref
    for log in src.get("logs") or []:
        assert set(log) <= {"timestamp", "fields"}, log
    for key in ("tag",):
        for holder in (src, process):
            assert holder.get(key) is None or isinstance(holder.get(key), dict)
    return {
        "traceID": src["traceID"], "spanID": src["spanID"], "parentSpanID": src.get("parentSpanID") or "",
        "flags": _number(src.get("flags") or 0), "operationName": src["operationName"],
        "references": sorted((r["refType"], r["traceID"], r["spanID"]) for r in src.get("references") or []),
        "startTime": _number(src["startTime"]), "startTimeMillis": _number(src["startTimeMillis"]),
        "duration": _number(src["duration"]),
        "tags": tag_set(src.get("tags")), "tag": sorted(((k, type(v).__name__ if type(v) is bool else "", v) for k, v in (src.get("tag") or {}).items()), key=repr),
        "logs": sorted(((_number(l["timestamp"]), tag_set(l.get("fields"))) for l in src.get("logs") or []), key=repr),
        "process": {"serviceName": process["serviceName"], "tags": tag_set(process.get("tags")),
                    "tag": sorted((process.get("tag") or {}).items())},
    }


def legacy_trace(body):
    """Canonical legacy trace: spans keyed by spanID with their process resolved; warnings are not compared."""
    assert body.get("errors") is None, body
    (trace,) = body["data"]
    processes = trace["processes"]
    spans = []
    for s in trace["spans"]:
        proc = processes[s["processID"]]
        spans.append({
            "traceID": s["traceID"], "spanID": s["spanID"], "operationName": s["operationName"],
            "flags": _number(s.get("flags", 0)),
            "references": sorted((r["refType"], r["traceID"], r["spanID"]) for r in s.get("references") or []),
            "startTime": _number(s["startTime"]), "duration": _number(s["duration"]),
            "tags": tag_set(s.get("tags")),
            "logs": sorted(((_number(l["timestamp"]), tag_set(l.get("fields"))) for l in s.get("logs") or []), key=repr),
            "process": (proc["serviceName"], tag_set(proc.get("tags"))),
        })
    return trace["traceID"], sorted(spans, key=lambda s: s["spanID"])


def span_ids(result):
    return sorted(s["spanId"] for s in otlp_flat(result))


def trace_ids(result):
    return sorted({s["traceId"] for s in otlp_flat(result)})
