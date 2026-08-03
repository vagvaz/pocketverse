"""Optional telemetry for pocketverse.

Two independent layers:

1. **Durable local JSONL (always on, stdlib only).** Each session appends
   newline-delimited JSON records to ``<session.root>/events.jsonl``. Every
   record is written with a single ``O_APPEND`` write so concurrent writers
   (launcher, shim, supervisor) can never interleave or corrupt lines.
   Fields: ``timestamp``, ``session_id``, ``event``, ``level`` and an
   optional ``attributes`` map.

2. **Optional OpenTelemetry.** Activated only when the SDK is installed
   (``pip install -e '.[telemetry]'``) *and* ``telemetry.enabled`` is
   ``true``. Traces and logs are preferred. OTLP exporters are constructed
   without arguments so they honour the standard ``OTEL_*`` environment
   variables (``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_EXPORTER_OTLP_TRACES_*``,
   ``OTEL_EXPORTER_OTLP_LOGS_*``, ``OTEL_EXPORTER_OTLP_HEADERS``, ...).
   When the SDK is absent or provider setup fails, every OpenTelemetry call
   degrades to a no-op and the local JSONL layer keeps working.

Lifecycle: ``start_session(...)`` / ``end_session(...)`` create and tear
down a :class:`TelemetrySession` (also usable standalone). Telemetry is
never a hard dependency and ``emit_event`` is always safe to call.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from .models import TelemetryConfig

__all__ = [
    "TelemetrySession",
    "start_session",
    "end_session",
    "emit_event",
    "start_span",
    "read_events",
]

# The module-global "active" session used by the module-level API so callers
# can do `emit_event("x")` without threading a handle through everything.
_ACTIVE: TelemetrySession | None = None

# stdlib logging.LogRecord attribute names; `extra` cannot override these.
_LOG_RESERVED_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_service_name(config: TelemetryConfig) -> str:
    """`OTEL_SERVICE_NAME` wins over the config value (standard semantics)."""
    return os.environ.get("OTEL_SERVICE_NAME") or config.service_name


# ---------------------------------------------------------------------------
# OpenTelemetry (lazy; everything here is a no-op when the SDK is absent)
# ---------------------------------------------------------------------------


def _import_otlp_exporters() -> tuple[Any, Any]:
    """Return ``(OTLPSpanExporter, OTLPLogExporter)`` classes or ``(None, None)``.

    Prefers the gRPC transport, falls back to HTTP. Constructing these with
    no arguments makes them honour the standard ``OTEL_*`` environment
    variables.
    """
    try:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        return OTLPSpanExporter, OTLPLogExporter
    except ImportError:
        pass
    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        return OTLPSpanExporter, OTLPLogExporter
    except ImportError:
        return None, None


def _load_otel() -> SimpleNamespace | None:
    """Import the OpenTelemetry API/SDK pieces, or return ``None`` if absent.

    Importing this module never triggers the ``opentelemetry`` packages; they
    are only imported here, lazily, once telemetry is actually enabled.
    """
    try:
        from opentelemetry import trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        try:
            # Public locations (opentelemetry-sdk >= 1.30).
            from opentelemetry.sdk.logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk.logs.export import BatchLogRecordProcessor
        except ImportError:  # pragma: no cover - older SDKs
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    except ImportError:
        return None

    span_exporter, log_exporter = _import_otlp_exporters()
    return SimpleNamespace(
        trace=trace,
        set_logger_provider=set_logger_provider,
        Resource=Resource,
        TracerProvider=TracerProvider,
        BatchSpanProcessor=BatchSpanProcessor,
        LoggerProvider=LoggerProvider,
        LoggingHandler=LoggingHandler,
        BatchLogRecordProcessor=BatchLogRecordProcessor,
        span_exporter=span_exporter,
        log_exporter=log_exporter,
    )


# ---------------------------------------------------------------------------
# TelemetrySession
# ---------------------------------------------------------------------------


class TelemetrySession:
    """Per-session telemetry sink: durable JSONL + optional OpenTelemetry.

    The JSONL layer is always active. OpenTelemetry is wired up only when
    ``config.enabled`` is true and the SDK is importable; otherwise every
    OpenTelemetry-backed call degrades to a no-op while JSONL keeps working.
    """

    def __init__(
        self,
        session_id: str,
        root: str | Path,
        config: TelemetryConfig | None = None,
    ) -> None:
        self.session_id = session_id
        self.root = Path(root)
        self.config = config or TelemetryConfig()
        self.events_path = self.root / "events.jsonl"

        self._otel: SimpleNamespace | None = None
        self._trace_provider: Any = None
        self._logger_provider: Any = None
        self._meter_provider: Any = None
        self._meter: Any = None
        self._tracer: Any = None
        self._logging_logger: logging.Logger | None = None
        self._shutdown = False
        self._dir_ready = False

        if self.config.enabled:
            self._init_otel()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "TelemetrySession":
        """Emit the ``session_started`` marker and return self."""
        self.emit_event("session_started")
        return self

    def shutdown(self) -> None:
        """Flush and release OpenTelemetry providers (JSONL stays untouched).

        Idempotent; ``emit_event`` is a no-op after this.
        """
        if self._shutdown:
            return
        self._shutdown = True
        for provider in (self._trace_provider, self._logger_provider, self._meter_provider):
            if provider is None:
                continue
            try:
                provider.shutdown()
            except Exception:
                pass

    # -- JSONL --------------------------------------------------------------

    def _ensure_dir(self) -> None:
        if not self._dir_ready:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            self._dir_ready = True

    def emit_event(
        self,
        event: str,
        *,
        level: str = "info",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Append one record to the local JSONL and mirror it to OpenTelemetry.

        Never raises: any failure in either layer is swallowed so telemetry
        can never take down the sandbox.
        """
        if self._shutdown:
            return
        try:
            self._write_jsonl(event, level, attributes)
        except Exception:
            pass
        if self._otel is not None:
            try:
                self._emit_otel_log(event, level, attributes)
            except Exception:
                pass

    def _write_jsonl(self, event: str, level: str, attributes: dict[str, Any] | None) -> None:
        """Append a single record with one O_APPEND write (atomic per line)."""
        self._ensure_dir()
        record: dict[str, Any] = {
            "timestamp": _now_iso(),
            "session_id": self.session_id,
            "event": event,
            "level": level,
        }
        if attributes:
            record["attributes"] = dict(attributes)
        line = json.dumps(record, ensure_ascii=False, default=str)
        fd = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)

    # -- OpenTelemetry ------------------------------------------------------

    def _init_otel(self) -> None:
        otel = _load_otel()
        if otel is None:
            return
        try:
            service_name = _resolve_service_name(self.config)
            resource = otel.Resource.create({
                "service.name": service_name,
                "service.instance.id": self.session_id,
                "session.id": self.session_id,
            })
            try:
                # Merge OTEL_RESOURCE_ATTRIBUTES / OTEL_SERVICE_NAME defaults.
                resource = resource.merge(otel.Resource.get_default())
            except Exception:
                pass

            if self.config.export_traces:
                try:
                    self._trace_provider = otel.TracerProvider(resource=resource)
                    if otel.span_exporter is not None:
                        self._trace_provider.add_span_processor(
                            otel.BatchSpanProcessor(otel.span_exporter())
                        )
                    otel.trace.set_tracer_provider(self._trace_provider)
                    self._tracer = otel.trace.get_tracer(service_name)
                except Exception:
                    self._trace_provider = None
                    self._tracer = None

            if self.config.export_logs:
                try:
                    self._logger_provider = otel.LoggerProvider(resource=resource)
                    if otel.log_exporter is not None:
                        self._logger_provider.add_log_record_processor(
                            otel.BatchLogRecordProcessor(otel.log_exporter())
                        )
                    otel.set_logger_provider(self._logger_provider)
                    self._logging_logger = logging.getLogger(
                        f"pocketverse.telemetry.{self.session_id}"
                    )
                    self._logging_logger.setLevel(logging.DEBUG)
                    self._logging_logger.propagate = False
                    self._logging_logger.addHandler(
                        otel.LoggingHandler(
                            level=logging.NOTSET, logger_provider=self._logger_provider
                        )
                    )
                except Exception:
                    self._logger_provider = None
                    self._logging_logger = None

            if self.config.export_metrics:
                self._init_otel_metrics(otel, resource)
        except Exception:
            pass
        self._otel = otel

    def _init_otel_metrics(self, otel: SimpleNamespace, resource: Any) -> None:
        try:
            from opentelemetry import metrics
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            try:
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                    OTLPMetricExporter,
                )
            except ImportError:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )
            self._meter_provider = MeterProvider(
                resource=resource,
                metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
            )
            metrics.set_meter_provider(self._meter_provider)
            self._meter = metrics.get_meter(_resolve_service_name(self.config))
        except Exception:
            self._meter_provider = None
            self._meter = None

    def _emit_otel_log(
        self,
        event: str,
        level: str,
        attributes: dict[str, Any] | None,
    ) -> None:
        if self._logging_logger is None:
            return
        levelno = getattr(logging, str(level).upper(), logging.INFO)
        extra: dict[str, Any] = {"event": event}
        for key, value in (attributes or {}).items():
            if key not in _LOG_RESERVED_ATTRS and key != "event":
                extra[key] = value
        try:
            self._logging_logger.log(levelno, event, extra=extra)
        except (ValueError, TypeError):  # extra key collided; drop extras
            self._logging_logger.log(levelno, event)

    @contextmanager
    def start_span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """Open an OpenTelemetry span; no-op (yields ``None``) when unavailable."""
        if self._otel is None or self._tracer is None:
            yield None
            return
        with self._tracer.start_as_current_span(name, attributes=attributes) as span:
            yield span


# ---------------------------------------------------------------------------
# Module-level lifecycle API (used by launcher / supervisor)
# ---------------------------------------------------------------------------


def start_session(
    session_id: str,
    root: str | Path,
    config: TelemetryConfig | None = None,
) -> TelemetrySession:
    """Create a telemetry session, emit ``session_started``, make it active."""
    session = TelemetrySession(session_id, root, config).start()
    global _ACTIVE
    _ACTIVE = session
    return session


def end_session(session: TelemetrySession | None = None) -> None:
    """Emit ``session_ended`` and shut the session down.

    Uses the active session when *session* is ``None``; no-op if there is
    none. Clears the active session when it matches the one being ended.
    """
    global _ACTIVE
    target = session if session is not None else _ACTIVE
    if target is None:
        return
    try:
        target.emit_event("session_ended")
    finally:
        target.shutdown()
    if _ACTIVE is target:
        _ACTIVE = None


def emit_event(
    event: str,
    *,
    level: str = "info",
    attributes: dict[str, Any] | None = None,
    session: TelemetrySession | None = None,
) -> None:
    """Emit an event to the active (or explicit) session. Safe always.

    No-ops when telemetry is unavailable (no active session and none given).
    """
    target = session if session is not None else _ACTIVE
    if target is None:
        return
    target.emit_event(event, level=level, attributes=attributes)


@contextmanager
def start_span(
    name: str,
    attributes: dict[str, Any] | None = None,
    *,
    session: TelemetrySession | None = None,
) -> Iterator[Any]:
    """Open an OpenTelemetry span on the active (or explicit) session.

    Yields ``None`` when telemetry is unavailable, so callers can always use
    ``with start_span("...") as span:``.
    """
    target = session if session is not None else _ACTIVE
    if target is None:
        yield None
        return
    with target.start_span(name, attributes=attributes) as span:
        yield span


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Read all records from a JSONL events file.

    Malformed or non-object lines are skipped; a missing file yields ``[]``.
    """
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except (FileNotFoundError, OSError):
        return []
    return records
