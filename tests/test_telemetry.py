"""Tests for pocketverse.telemetry — durable JSONL + optional OpenTelemetry.

The test environment has no opentelemetry packages installed, so the SDK
paths here exercise graceful degradation; tests that need a real SDK skip.
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
from pathlib import Path

import pytest
import yaml

from pocketverse import telemetry
from pocketverse.models import SandboxConfig, TelemetryConfig, load_config
from pocketverse.telemetry import (
    TelemetrySession,
    emit_event,
    end_session,
    read_events,
    start_session,
    start_span,
)

# ===========================================================================
# Fixtures / helpers
# ===========================================================================


@pytest.fixture(autouse=True)
def _clean_active_session():
    yield
    telemetry._ACTIVE = None


@pytest.fixture
def cfg(tmp_path: Path) -> SandboxConfig:
    return SandboxConfig(state_dir=tmp_path / "state")


def _events(root: Path) -> list[dict]:
    return read_events(root / "events.jsonl")


# ===========================================================================
# Local JSONL: append / read
# ===========================================================================


def test_jsonl_append_and_read(tmp_path: Path):
    root = tmp_path / "session-abc"
    ts = TelemetrySession("abc", root)
    ts.emit_event("sandbox_started", attributes={"mode": "allowlist"})
    ts.emit_event("agent_exit", level="info")

    events = _events(root)
    assert len(events) == 2

    first = events[0]
    assert set(first) == {"timestamp", "session_id", "event", "level", "attributes"}
    assert first["session_id"] == "abc"
    assert first["event"] == "sandbox_started"
    assert first["level"] == "info"
    assert first["attributes"] == {"mode": "allowlist"}
    assert first["timestamp"]  # ISO-8601 timestamp present

    second = events[1]
    assert second["event"] == "agent_exit"
    assert second["level"] == "info"
    assert "attributes" not in second


def test_jsonl_written_to_session_root(tmp_path: Path):
    root = tmp_path / "state" / "test" / "session-x"
    ts = TelemetrySession("x", root)
    ts.emit_event("hi")
    assert (root / "events.jsonl").exists()
    assert [r["event"] for r in _events(root)] == ["hi"]


def test_jsonl_append_is_durable_single_write(tmp_path: Path):
    """Concurrent emitters must never corrupt/interleave records (O_APPEND)."""
    root = tmp_path / "sess"
    ts = TelemetrySession("sess", root)
    errors: list[Exception] = []

    def worker(i: int):
        try:
            for j in range(50):
                ts.emit_event(f"t{i}-{j}", attributes={"i": i, "j": j})
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    records = _events(root)
    assert len(records) == 200
    assert len({r["event"] for r in records}) == 200  # no lost/dup records
    assert all(r["session_id"] == "sess" for r in records)


def test_emit_non_json_serializable_attributes(tmp_path: Path):
    class Unserializable:
        def __str__(self) -> str:
            return "obj!"

    ts = TelemetrySession("s", tmp_path)
    ts.emit_event("e", attributes={"thing": Unserializable(), "path": Path("/tmp/x")})
    record = _events(tmp_path)[0]
    assert record["attributes"]["thing"] == "obj!"
    assert record["attributes"]["path"] == "/tmp/x"


def test_emit_never_raises_on_jsonl_failure(tmp_path: Path, monkeypatch):
    ts = TelemetrySession("s", tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(TelemetrySession, "_write_jsonl", boom)
    ts.emit_event("e")  # must not raise


# ===========================================================================
# Local JSONL: malformed-safe reads
# ===========================================================================


def test_read_events_skips_malformed_lines(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "{not json}\n"
        "12345\n"
        + json.dumps({"timestamp": "t", "session_id": "s", "event": "ok", "level": "info"})
        + "\n"
        "\x00\xff garbage\n"
        '{"event": "only-event"}\n'
        "\n",  # blank line
        encoding="utf-8",
    )
    records = read_events(path)
    assert [r["event"] for r in records] == ["ok", "only-event"]


def test_read_events_missing_file_is_empty(tmp_path: Path):
    assert read_events(tmp_path / "does-not-exist.jsonl") == []


def test_read_events_ignores_partial_tail_line(tmp_path: Path):
    """A torn final line (crash mid-write) must not break parsing."""
    path = tmp_path / "events.jsonl"
    good = json.dumps({"timestamp": "t", "session_id": "s", "event": "ok", "level": "info"})
    path.write_text(good + "\n" + '{"timestamp": "t", "session_id"', encoding="utf-8")
    assert [r["event"] for r in read_events(path)] == ["ok"]


# ===========================================================================
# Disabled / no-SDK graceful behavior
# ===========================================================================


def test_disabled_config_still_writes_jsonl(tmp_path: Path):
    ts = TelemetrySession("s", tmp_path, config=TelemetryConfig(enabled=False))
    ts.emit_event("local_only")
    assert ts._otel is None
    assert [r["event"] for r in _events(tmp_path)] == ["local_only"]


def test_enabled_without_sdk_is_graceful(tmp_path: Path):
    if telemetry._load_otel() is not None:
        pytest.skip("opentelemetry SDK is installed; no-SDK path unavailable")
    ts = TelemetrySession("s", tmp_path, config=TelemetryConfig(enabled=True))
    assert ts._otel is None
    ts.emit_event("still_local")
    assert [r["event"] for r in _events(tmp_path)] == ["still_local"]


def test_broken_otel_bundle_degrades(tmp_path: Path, monkeypatch):
    """Provider init that throws must degrade to JSONL-only, no crash."""

    def broken() -> object:
        return object()  # no Resource/trace attributes

    monkeypatch.setattr(telemetry, "_load_otel", broken)
    ts = TelemetrySession("s", tmp_path, config=TelemetryConfig(enabled=True))
    ts.emit_event("ok")
    assert [r["event"] for r in _events(tmp_path)] == ["ok"]


def test_otel_log_failure_is_swallowed(tmp_path: Path):
    ts = TelemetrySession("s", tmp_path)
    ts._otel = object()

    def boom(*a, **k):
        raise RuntimeError("otel down")

    ts._emit_otel_log = boom  # type: ignore[method-assign]
    ts.emit_event("e")  # must not raise
    assert [r["event"] for r in _events(tmp_path)] == ["e"]


def test_shutdown_is_idempotent_and_stops_writes(tmp_path: Path):
    ts = TelemetrySession("s", tmp_path)
    ts.emit_event("before")
    ts.shutdown()
    ts.emit_event("after")
    ts.shutdown()  # idempotent
    assert [r["event"] for r in _events(tmp_path)] == ["before"]


def test_start_span_without_otel_yields_none(tmp_path: Path):
    ts = TelemetrySession("s", tmp_path)
    with ts.start_span("work") as span:
        assert span is None
    with start_span("mod") as span:
        assert span is None


# ===========================================================================
# Module-level lifecycle API
# ===========================================================================


def test_start_end_session_lifecycle(tmp_path: Path):
    root = tmp_path / "session-l1"
    ts = start_session("l1", root)
    assert telemetry._ACTIVE is ts
    emit_event("hello", level="debug", attributes={"n": 1})
    end_session()

    events = _events(root)
    assert [r["event"] for r in events] == ["session_started", "hello", "session_ended"]
    assert events[1]["level"] == "debug"
    assert events[1]["attributes"] == {"n": 1}
    assert telemetry._ACTIVE is None


def test_end_session_explicit_session(tmp_path: Path):
    root = tmp_path / "session-l2"
    ts = start_session("l2", root)
    end_session(ts)
    assert [r["event"] for r in _events(root)] == ["session_started", "session_ended"]
    assert telemetry._ACTIVE is None


def test_start_session_creates_missing_root(tmp_path: Path):
    root = tmp_path / "nested" / "session-l3"
    ts = start_session("l3", root)
    emit_event("first")
    end_session(ts)
    assert [r["event"] for r in _events(root)] == ["session_started", "first", "session_ended"]


def test_emit_event_without_session_is_noop():
    assert telemetry._ACTIVE is None
    emit_event("orphan")  # must not raise
    end_session()  # must not raise


def test_emit_event_explicit_session(tmp_path: Path):
    ts = TelemetrySession("s", tmp_path)
    emit_event("x", session=ts)
    assert [r["event"] for r in _events(tmp_path)] == ["x"]


# ===========================================================================
# Config parsing / serialization
# ===========================================================================


def test_telemetry_config_defaults():
    cfg = TelemetryConfig()
    assert cfg.enabled is False
    assert cfg.service_name == "pocketverse"
    assert cfg.export_logs is True
    assert cfg.export_traces is True
    assert cfg.export_metrics is False


def test_telemetry_config_json_serializable():
    cfg = TelemetryConfig(enabled=True, service_name="svc")
    data = json.loads(cfg.model_dump_json())
    assert data == {
        "enabled": True,
        "service_name": "svc",
        "export_logs": True,
        "export_traces": True,
        "export_metrics": False,
    }
    assert TelemetryConfig.model_validate(data) == cfg


def test_sandbox_config_telemetry_roundtrip(tmp_path: Path):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        yaml.safe_dump(
            {"telemetry": {"enabled": True, "service_name": "my-agent", "export_metrics": True}}
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.telemetry.enabled is True
    assert cfg.telemetry.service_name == "my-agent"
    assert cfg.telemetry.export_metrics is True
    assert cfg.telemetry.export_logs is True  # default preserved


def test_sandbox_config_telemetry_defaults():
    cfg = SandboxConfig()
    assert isinstance(cfg.telemetry, TelemetryConfig)
    assert cfg.telemetry.enabled is False


def test_telemetry_config_yaml_roundtrip(tmp_path: Path):
    cfg = TelemetryConfig(enabled=True, service_name="svc")
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(cfg.model_dump()), encoding="utf-8")
    loaded = TelemetryConfig.model_validate(yaml.safe_load(path.read_text()))
    assert loaded == cfg


# ===========================================================================
# Optional imports / env handling
# ===========================================================================


def test_module_import_does_not_eagerly_load_opentelemetry():
    before = {m for m in sys.modules if m == "opentelemetry" or m.startswith("opentelemetry.")}
    importlib.reload(telemetry)
    after = {m for m in sys.modules if m == "opentelemetry" or m.startswith("opentelemetry.")}
    assert after == before


def test_otel_service_name_env(tmp_path: Path, monkeypatch):
    cfg = TelemetryConfig(service_name="pocketverse")
    assert telemetry._resolve_service_name(cfg) == "pocketverse"
    monkeypatch.setenv("OTEL_SERVICE_NAME", "env-svc")
    assert telemetry._resolve_service_name(cfg) == "env-svc"


def test_otlp_exporter_construction_honors_env(tmp_path: Path):
    if telemetry._load_otel() is None:
        pytest.skip("opentelemetry SDK not installed")
    span_cls, log_cls = telemetry._import_otlp_exporters()
    assert span_cls is not None and log_cls is not None
    # Constructing with no args must succeed and pick up OTEL_* env vars.
    span_cls()
    log_cls()
