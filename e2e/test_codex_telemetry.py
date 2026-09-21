"""Run the production Codex log pipeline with a real, locally supplied collector.

Set OTELCOL_TEST_BIN to the official 0.116.1 release's otelcol-contrib binary.
Only the exporter and receiver/health ports are replaced. No AWS client or model
is used. Values are synthetic; the wire shape follows Codex 0.155.1, including
its zero timeUnixNano and populated observedTimeUnixNano.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest
import yaml


CONFIG = Path(__file__).resolve().parents[1] / "coding-agents/codex/otel-collector-config.yaml"
COUNTERS = {
    "input_token_count": "120",
    "output_token_count": "30",
    "cached_token_count": 80,
    "cache_write_token_count": 10,
    "reasoning_token_count": 12,
}
PRIVATE_MARKER = "OFFLINE_TEST_PRIVATE_CONTENT"
RESOURCE = {
    "service.name": "codex_exec",
    "service.version": "0.155.1",
    "user.id": "offline-user",
    "team.id": "offline-team",
    "agent.id": "codex",
    "run.id": "offline-run",
    "session.id": "offline-runtime-session",
}


def _value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": value}


def _attributes(values):
    return [{"key": key, "value": _value(value)} for key, value in values.items()]


def _completion(identifier, *, counters=None, timestamp=0, observed=None):
    return {
        "timeUnixNano": str(timestamp),
        "observedTimeUnixNano": str(observed if observed is not None else time.time_ns()),
        "severityNumber": 9,
        "severityText": "INFO",
        "body": None,
        "attributes": _attributes({
            "event.name": "codex.sse_event",
            "event.kind": "response.completed",
            "event.timestamp": "2026-09-21T00:00:00Z",
            "conversation.id": identifier,
            "app.version": "0.155.1",
            "model": "offline-model",
            **(COUNTERS if counters is None else counters),
        }),
        "droppedAttributesCount": 0,
        "flags": 0,
        "traceId": "",
        "spanId": "",
        "eventName": "",
    }


def _attribute_map(record):
    return {item["key"]: item["value"] for item in record.get("attributes", [])}


@dataclass
class Collector:
    process: subprocess.Popen
    endpoint: str
    output: Path
    log: Path
    http: urllib.request.OpenerDirector

    def send(self, records):
        payload = {
            "resourceLogs": [{
                "resource": {"attributes": _attributes({
                    **RESOURCE,
                    "host.name": PRIVATE_MARKER,
                    "env": PRIVATE_MARKER,
                    "prompt": PRIVATE_MARKER,
                })},
                "scopeLogs": [{
                    "scope": {"name": "codex_otel", "version": "0.155.1"},
                    "logRecords": records,
                }],
            }],
        }
        request = urllib.request.Request(
            self.endpoint + "/v1/logs",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.http.open(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def records(self):
        if not self.output.exists():
            return []
        records = []
        for line in self.output.read_text().splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue  # The collector may currently be writing the final line.
            for group in payload.get("resourceLogs", []):
                resource = group.get("resource", {})
                for scope in group.get("scopeLogs", []):
                    records.extend((resource, record) for record in scope.get("logRecords", []))
        return records

    def matching(self, identifier):
        return [(resource, record) for resource, record in self.records()
                if _attribute_map(record).get("conversation.id") == _value(identifier)]

    def wait_for(self, identifier):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            found = self.matching(identifier)
            if found:
                return found
            assert self.process.poll() is None, self.log.read_text()
            time.sleep(0.05)
        pytest.fail(f"No exported record for {identifier}; collector log:\n{self.log.read_text()}")


@pytest.fixture(scope="module")
def collector(tmp_path_factory, record_testsuite_property):
    supplied = os.environ.get("OTELCOL_TEST_BIN")
    if not supplied:
        pytest.skip("Set OTELCOL_TEST_BIN to the official 0.116.1 collector binary for the real offline pipeline test.")
    binary = Path(supplied).expanduser().resolve()
    assert binary.is_file() and os.access(binary, os.X_OK), (
        f"OTELCOL_TEST_BIN must name an executable collector: {binary}"
    )
    version = subprocess.run([str(binary), "--version"], text=True, capture_output=True, timeout=10)
    assert version.returncode == 0, version.stderr
    # Record the binary's actual build label. The supplied 0.116.1 release asset
    # reports 0.116.0; do not relabel it or claim an unobserved version.
    record_testsuite_property("otelcol_version_output", version.stdout.strip())
    record_testsuite_property("otelcol_binary_sha256", hashlib.sha256(binary.read_bytes()).hexdigest())
    production_bytes = CONFIG.read_bytes()
    record_testsuite_property("codex_production_config_sha256", hashlib.sha256(production_bytes).hexdigest())

    directory = tmp_path_factory.mktemp("codex-collector")
    output, log = directory / "export.jsonl", directory / "collector.log"
    config = copy.deepcopy(yaml.safe_load(production_bytes))
    with socket.socket() as receiver, socket.socket() as health:
        receiver.bind(("127.0.0.1", 0))
        health.bind(("127.0.0.1", 0))
        receiver_port, health_port = receiver.getsockname()[1], health.getsockname()[1]
    config["receivers"]["otlp"]["protocols"]["http"]["endpoint"] = f"127.0.0.1:{receiver_port}"
    config["extensions"]["health_check"]["endpoint"] = f"127.0.0.1:{health_port}"
    config["exporters"] = {"file/offline": {
        "path": str(output), "format": "json", "flush_interval": "100ms",
    }}
    config["service"]["pipelines"]["logs"]["exporters"] = ["file/offline"]
    fixture_config = directory / "collector.yaml"
    fixture_config.write_text(yaml.safe_dump(config, sort_keys=False))
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # No inherited AWS/model credentials or exporters can reach this subprocess.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    with log.open("w") as writer:
        process = subprocess.Popen(
            [str(binary), "--config", str(fixture_config)],
            stdout=writer, stderr=subprocess.STDOUT, env=env,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert process.poll() is None, log.read_text()
                try:
                    with http.open(f"http://127.0.0.1:{health_port}", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.05)
            else:
                pytest.fail(f"Collector never became healthy:\n{log.read_text()}")
            yield Collector(process, f"http://127.0.0.1:{receiver_port}", output, log, http)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def test_only_usage_bearing_completion_is_exported_with_identity_and_numeric_tokens(collector):
    observed = time.time_ns() - 2_000_000_000
    completion = _completion("native-completion", observed=observed)
    completion["attributes"] += _attributes({
        "prompt": PRIVATE_MARKER, "tool.arguments": PRIVATE_MARKER,
        "tool.output": PRIVATE_MARKER, "unrelated.attribute": PRIVATE_MARKER,
    })
    # Codex emits a raw response.completed too; it has no usage and must not
    # double-count the same conversation's usage-bearing completion.
    raw_completion = _completion("native-completion", counters={})
    rejected = []
    for event in ("codex.api_request", "codex.user_prompt", "codex.tool_result", "codex.tool_decision"):
        record = _completion("rejected-" + event)
        record["body"] = {"stringValue": PRIVATE_MARKER}
        for attribute in record["attributes"]:
            if attribute["key"] == "event.name":
                attribute["value"] = _value(event)
        rejected.append(record)
    errored = _completion("rejected-error")
    errored["attributes"] += _attributes({"error.message": PRIVATE_MARKER})
    status, response = collector.send([completion, raw_completion, *rejected, errored])
    assert status == 200, response
    rows = collector.wait_for("native-completion")
    assert len(rows) == 1
    resource, record = rows[0]
    assert _attribute_map(resource) == {key: _value(value) for key, value in RESOURCE.items()}
    attrs = _attribute_map(record)
    for field, expected in COUNTERS.items():
        assert attrs[field] == {"intValue": str(expected)}, (field, attrs[field])
    assert int(record["timeUnixNano"]) == observed
    assert int(record["observedTimeUnixNano"]) == observed
    assert record.get("body") in (None, {})
    assert PRIVATE_MARKER not in json.dumps(collector.records())
    assert not any(collector.matching("rejected-" + event) for event in (
        "codex.api_request", "codex.user_prompt", "codex.tool_result", "codex.tool_decision", "error",
    ))


def test_explicit_zero_missing_fields_and_existing_event_time_stay_distinct(collector):
    zero = _completion("explicit-zero", counters={
        "input_token_count": "0", "output_token_count": "0",
        "cached_token_count": 0, "cache_write_token_count": 0, "reasoning_token_count": 0,
    })
    missing = _completion("unreported-fields", counters={"input_token_count": "12"})
    original = time.time_ns() - 3_000_000_000
    existing_time = _completion("existing-time", timestamp=original, observed=original + 1_000_000)
    status, response = collector.send([zero, missing, existing_time])
    assert status == 200, response
    collector.wait_for("existing-time")
    zero_attrs = _attribute_map(collector.matching("explicit-zero")[0][1])
    assert all(zero_attrs[field] == {"intValue": "0"} for field in COUNTERS)
    missing_attrs = _attribute_map(collector.matching("unreported-fields")[0][1])
    assert missing_attrs["input_token_count"] == {"intValue": "12"}
    assert not (set(COUNTERS) - {"input_token_count"}) & missing_attrs.keys()
    assert int(collector.matching("existing-time")[0][1]["timeUnixNano"]) == original


def test_all_string_counters_are_exported_as_exact_numbers_including_zero(collector):
    cases = {
        "all-string-counters": {field: str(value) for field, value in COUNTERS.items()},
        "all-string-zero": {field: "0" for field in COUNTERS},
    }
    status, response = collector.send([
        _completion(identifier, counters=counters) for identifier, counters in cases.items()
    ])
    assert status == 200, response
    collector.wait_for("all-string-zero")
    for identifier, expected in cases.items():
        rows = collector.matching(identifier)
        assert len(rows) == 1
        attrs = _attribute_map(rows[0][1])
        for field, value in expected.items():
            assert attrs[field] == {"intValue": value}, (identifier, field, attrs[field])


@pytest.mark.parametrize("field,invalid", [
    ("input_token_count", "not-a-counter"),
    ("input_token_count", ""),
    ("input_token_count", False),
    ("input_token_count", 1.5),
    ("input_token_count", "-1"),
    ("output_token_count", "not-a-counter"),
    ("cached_token_count", "not-a-counter"),
    ("cache_write_token_count", -1),
    ("reasoning_token_count", False),
])
def test_malformed_counter_is_rejected_or_unavailable_never_coerced_to_usage(collector, field, invalid):
    identifier = "invalid-" + field + "-" + repr(invalid)
    counters = {**COUNTERS, field: invalid}
    collector.send([_completion(identifier, counters=counters)])
    # A subsequent valid record proves the batch/export flush completed. Rejected
    # records may be dropped or lose the malformed field; they must not export a
    # value Logs Insights can silently sum as zero, truncate, or offset negatives.
    control = identifier + "-control"
    status, response = collector.send([_completion(control)])
    assert status == 200, response
    collector.wait_for(control)
    for _resource, record in collector.matching(identifier):
        assert field not in _attribute_map(record), (
            f"Malformed {field}={invalid!r} was exported as {_attribute_map(record)[field]!r}"
        )


def test_int64_boundary_is_exact_and_overflow_is_rejected_or_unavailable(collector):
    maximum = str((1 << 63) - 1)
    overflow = str(1 << 63)
    boundary = _completion("int64-boundary", counters={field: maximum for field in COUNTERS})
    status, response = collector.send([boundary])
    assert status == 200, response
    boundary_attrs = _attribute_map(collector.wait_for("int64-boundary")[0][1])
    for field in COUNTERS:
        assert boundary_attrs[field] == {"intValue": maximum}, (field, boundary_attrs[field])

    for field in COUNTERS:
        collector.send([_completion("overflow-" + field, counters={**COUNTERS, field: overflow})])
    # A valid record after the invalid payloads proves the exporter has flushed.
    status, response = collector.send([_completion("overflow-control")])
    assert status == 200, response
    collector.wait_for("overflow-control")
    for field in COUNTERS:
        for _resource, record in collector.matching("overflow-" + field):
            assert field not in _attribute_map(record), (
                f"Out-of-range {field}={overflow} was exported as {_attribute_map(record)[field]!r}"
            )
