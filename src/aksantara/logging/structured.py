"""Structured JSON logging for Aksantara.

Provides JSON output with timestamp/level/loggerName/message plus
request-scoped context (request_id, trace_id, run_id) via contextvars,
and PII redaction for sensitive keys.

Backends:
- primary: ``structlog`` with ``JSONRenderer`` (preferred)
- fallback: stdlib ``logging`` + ``python-json-logger`` JsonFormatter
- ultimate fallback: plain stdlib if neither library is installed

Usage::

    from aksantara.logging.structured import configure_structured_logging, get_logger

    configure_structured_logging()  # idempotent, call once at startup
    log = get_logger(__name__)
    log.info("embedding_done", model="gemini-embedding-001", dimensions=768)

Context binding is done automatically by FastAPI middleware; manual binding::

    from aksantara.logging.structured import bind_context, clear_context
    bind_context(request_id="req-123", run_id="run-abc")
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Context vars — request-scoped fields injected into every log line
# ---------------------------------------------------------------------------

_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aksantara_request_id", default=None
)
_trace_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aksantara_trace_id", default=None
)
_run_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aksantara_run_id", default=None
)

# ---------------------------------------------------------------------------
# Redaction — scrub sensitive values before emission
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "session",
        "private_key",
        "access_token",
        "refresh_token",
    }
)

_SENSITIVE_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|authorization|cookie|session|private[_-]?key)",
    re.IGNORECASE,
)


def _redact_value(key: str, value: Any) -> Any:
    if isinstance(key, str) and _SENSITIVE_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        # Also scrub inline bearer tokens / secrets that look like high-entropy strings.
        # Keep this conservative: only redact if key is sensitive; values alone are left
        # as-is to avoid false positives on content hashes.
        return value
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, v) for v in value]
    return value


def _redact_event_dict(event_dict: dict[str, Any]) -> dict[str, Any]:
    for k in list(event_dict.keys()):
        # Never redact structural log keys themselves; redact their values if key is sensitive.
        if k in {
            "event",
            "message",
            "msg",
            "level",
            "logger",
            "timestamp",
            "request_id",
            "trace_id",
            "run_id",
        }:
            continue
        event_dict[k] = _redact_value(k, event_dict[k])
    return event_dict


# ---------------------------------------------------------------------------
# structlog processors
# ---------------------------------------------------------------------------


def _add_contextvars(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    rid = _request_id_ctx.get()
    tid = _trace_id_ctx.get()
    ruid = _run_id_ctx.get()
    if rid is not None:
        event_dict.setdefault("request_id", rid)
    if tid is not None:
        event_dict.setdefault("trace_id", tid)
    if ruid is not None:
        event_dict.setdefault("run_id", ruid)
    return event_dict


def _redact_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    return _redact_event_dict(event_dict)


# ---------------------------------------------------------------------------
# Public context helpers
# ---------------------------------------------------------------------------


def bind_context(
    *,
    request_id: str | None = None,
    trace_id: str | None = None,
    run_id: str | None = None,
) -> None:
    """Bind request-scoped fields for the current context."""
    if request_id is not None:
        _request_id_ctx.set(request_id)
    if trace_id is not None:
        _trace_id_ctx.set(trace_id)
    if run_id is not None:
        _run_id_ctx.set(run_id)


def clear_context() -> None:
    _request_id_ctx.set(None)
    _trace_id_ctx.set(None)
    _run_id_ctx.set(None)


def get_context() -> dict[str, str | None]:
    return {
        "request_id": _request_id_ctx.get(),
        "trace_id": _trace_id_ctx.get(),
        "run_id": _run_id_ctx.get(),
    }


# ---------------------------------------------------------------------------
# Configuration — idempotent, safe to call multiple times
# ---------------------------------------------------------------------------

_configured: bool = False


def _stdio_handler() -> logging.Handler:
    h = logging.StreamHandler(sys.stdout)
    h.setLevel(logging.NOTSET)
    return h


def _json_formatter() -> logging.Formatter:
    # Try python-json-logger first; fall back to plain JSON via stdlib.
    try:
        from pythonjsonlogger import jsonlogger  # type: ignore[import-untyped]

        # python-json-logger 2.x : JsonFormatter
        fmt = "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s %(trace_id)s %(run_id)s"
        formatter: logging.Formatter = jsonlogger.JsonFormatter(  # type: ignore[no-untyped-call]
            fmt,
            rename_fields={
                "asctime": "timestamp",
                "levelname": "level",
                "name": "logger",
                "message": "message",
            },
            json_ensure_ascii=False,
        )
        return formatter
    except Exception:
        pass

    class _FallbackJsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload: dict[str, Any] = {
                "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%SZ"),
                "level": record.levelname.lower(),
                "logger": record.name,
                "message": record.getMessage(),
            }
            rid = getattr(record, "request_id", None) or _request_id_ctx.get()
            tid = getattr(record, "trace_id", None) or _trace_id_ctx.get()
            ruid = getattr(record, "run_id", None) or _run_id_ctx.get()
            if rid:
                payload["request_id"] = rid
            if tid:
                payload["trace_id"] = tid
            if ruid:
                payload["run_id"] = ruid
            # Include any extra fields passed via `extra` or `record.__dict__`
            for k, v in record.__dict__.items():
                if k in payload or k.startswith("_"):
                    continue
                if k in {
                    "args",
                    "asctime",
                    "created",
                    "exc_info",
                    "exc_text",
                    "filename",
                    "funcName",
                    "levelname",
                    "levelno",
                    "lineno",
                    "module",
                    "msecs",
                    "msg",
                    "name",
                    "pathname",
                    "process",
                    "processName",
                    "relativeCreated",
                    "stack_info",
                    "thread",
                    "threadName",
                }:
                    continue
                payload[k] = _redact_value(k, v)
            if record.exc_info and record.exc_info[0] is not None:
                payload["exc_info"] = self.formatException(record.exc_info)
            # Redact before serializing
            payload = _redact_event_dict(payload)
            return json.dumps(payload, ensure_ascii=False)

    return _FallbackJsonFormatter()


def configure_structured_logging(
    *,
    level: int | None = None,
    json_output: bool | None = None,
    force: bool = False,
) -> None:
    """Configure root + uvicorn loggers for JSON structured output.

    Idempotent unless ``force=True``. Reads ``AKSANTARA_LOG_LEVEL`` and
    ``AKSANTARA_LOG_JSON`` (default: json when not tty or when env forces it)
    to stay compatible with Cloud Run.
    """
    global _configured
    if _configured and not force:
        return

    raw_level = os.getenv("AKSANTARA_LOG_LEVEL", "")
    if level is None:
        level = logging.INFO
        if raw_level:
            level = getattr(logging, raw_level.upper(), logging.INFO)

    if json_output is None:
        env_json = os.getenv("AKSANTARA_LOG_JSON", "").lower()
        if env_json in {"1", "true", "yes"}:
            json_output = True
        elif env_json in {"0", "false", "no"}:
            json_output = False
        else:
            # Default: JSON in non-interactive / Cloud Run, plain on tty for local dev
            json_output = not sys.stdout.isatty()

    formatter: logging.Formatter | None = _json_formatter() if json_output else None

    # Try structlog path first
    try:
        import structlog  # type: ignore[import-untyped]

        timestamper = structlog.processors.TimeStamper(
            fmt="iso", utc=True, key="timestamp"
        )
        shared_processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            _add_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            _redact_processor,
        ]

        structlog.configure(
            processors=[
                *shared_processors,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        # Route stdlib -> structlog via ProcessorFormatter
        # foreign_pre_chain must not contain filter_by_level (requires logger instance)
        foreign_processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            _add_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            _redact_processor,
        ]
        stdlib_formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=foreign_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(ensure_ascii=False)
                if json_output
                else structlog.dev.ConsoleRenderer(colors=False),
            ],
        )
        handler = _stdio_handler()
        handler.setFormatter(stdlib_formatter)
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(level)

        # Re-attach for uvicorn/fastapi loggers so they also emit JSON
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
            lg = logging.getLogger(name)
            lg.handlers.clear()
            lg.addHandler(handler)
            lg.setLevel(level)
            lg.propagate = False

        _configured = True
        return
    except Exception:
        # Fall through to stdlib json formatter
        pass

    # Fallback: stdlib only
    handler = _stdio_handler()
    if formatter is not None:
        handler.setFormatter(formatter)
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.addHandler(handler)
        lg.setLevel(level)
        lg.propagate = False
    _configured = True


# ---------------------------------------------------------------------------
# get_logger — thin wrapper so callers don't need to know the backend
# ---------------------------------------------------------------------------


def get_logger(name: str | None = None) -> Any:
    """Return a structured logger for *name*.

    Prefers ``structlog.get_logger`` with bound contextvars; falls back to
    stdlib ``logging.getLogger`` with a JsonFormatter-backed handler already
    configured by :func:`configure_structured_logging`.
    """
    # Ensure minimal configuration at first use (safe for tests that never call configure)
    if not _configured:
        try:
            configure_structured_logging()
        except Exception:
            pass
    try:
        import structlog  # type: ignore[import-untyped]

        # structlog available and configured?
        return structlog.get_logger(name)
    except Exception:
        return logging.getLogger(name)


__all__ = [
    "bind_context",
    "clear_context",
    "configure_structured_logging",
    "get_context",
    "get_logger",
]
