"""Structured logging configuration.

Call :func:`setup` exactly once at process startup (the FastAPI lifespan hook
in ``road_safety.server`` does this) to configure Python's root logger with
output suitable for production log aggregators (ELK, Datadog, CloudWatch).
Every log line is emitted as a compact single-line JSON object so aggregators
can parse the level, timestamp, logger name, and message without custom
regex — this is the industry norm for 12-factor apps.

When iterating locally the JSON wall is hard to read, so
``ROAD_LOG_FORMAT=text`` switches to a human-friendly multi-field text
formatter instead. The JSON path remains the production default.

Python concept — the ``logging`` module:
    The standard library ships a hierarchical logger system. Each module
    gets a named logger via ``logging.getLogger(__name__)``; logs flow *up*
    to the root logger, which is where handlers and formatters live.
    Configuring the root once (this module's job) means every module's
    logger automatically inherits the format — no per-module setup needed.

Layering note:
    ``logging.py`` intentionally has no dependency on :mod:`road_safety.config`
    so it can be imported extremely early in bootstrap (before env parsing is
    done) without circular-import risk. It reads its own two env vars
    directly.
"""

from __future__ import annotations

# Standard-library imports only — logging has no third-party dependencies
# and must be safe to import before anything else in the app is initialised.
import logging
import os
import sys
from datetime import datetime, timezone


class _JSONFormatter(logging.Formatter):
    """Minimal JSON-line formatter for log records.

    State:
        None. Each ``format()`` call is pure: it takes a ``LogRecord`` and
        returns a string. No buffering, no per-instance state. Formatters are
        shared across the process and must be thread-safe; statelessness is
        the simplest way to guarantee that.

    Lifecycle:
        Instantiated exactly once inside :func:`setup` and attached to the
        single stdout handler on the root logger. Never re-created per-line.

    Callers:
        Only :func:`setup` — this class is module-private (leading underscore
        by Python convention signals "do not import from elsewhere").

    Why hand-rolled instead of :func:`json.dumps`?
        Log formatting is on every hot-path request. Skipping ``json.dumps``
        and manually assembling the JSON payload avoids dict allocation and
        the dumps call overhead on every log line.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render a :class:`logging.LogRecord` as a JSON object line.

        Args:
            record: The record produced by the logging framework. It carries
                the log level, logger name, raw message, creation timestamp,
                and optional exception info.

        Returns:
            A single-line JSON string suitable for log aggregators. No
            trailing newline — :class:`logging.StreamHandler` adds it.
        """
        # ``record.created`` is a Unix epoch float. Convert to ISO-8601 UTC
        # so the log aggregator can index timestamps without guessing the
        # producer's timezone.
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        # ``getMessage()`` applies any ``%`` args that were passed to the
        # logging call (e.g. ``log.info("hello %s", name)``).
        msg = record.getMessage()
        # Manually build the JSON payload for speed; see class docstring.
        # f-strings are Python's interpolation syntax: any ``{expr}`` inside
        # an ``f"..."`` literal is evaluated and substituted.
        payload = (
            f'{{"ts":"{ts}","level":"{record.levelname}",'
            f'"logger":"{record.name}","msg":{self._quote(msg)}'
        )
        # When a logger is called with ``exc_info=True`` (or an exception was
        # in flight), include the formatted traceback so stack traces stay
        # attached to their originating log line.
        if record.exc_info and record.exc_info[0] is not None:
            payload += f',"exc":{self._quote(self.formatException(record.exc_info))}'
        payload += "}"
        return payload

    @staticmethod
    def _quote(s: str) -> str:
        """JSON-escape a raw string so it can be embedded as a quoted value.

        Escapes backslashes, double quotes, and newlines — the three
        characters that would otherwise break JSON parsing when the message
        contains stack traces or user input. Not a full JSON escape
        implementation (we skip control chars) but sufficient for log output.
        """
        # Order matters: escape backslashes *first* or the backslashes we add
        # for quotes / newlines will themselves get doubled.
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def setup(level: str | None = None) -> None:
    """Configure the root logger. Safe to call multiple times.

    Idempotent by design: clears existing handlers before installing a new
    one, so re-entry (e.g. test harnesses that re-import the app) does not
    produce duplicate log lines.

    Args:
        level: Optional override for the log level (e.g. ``"DEBUG"``).
            When ``None`` (the default) the ``ROAD_LOG_LEVEL`` env var is
            consulted, falling back to ``"INFO"``. Invalid names quietly
            degrade to ``INFO`` rather than raising.

    Returns:
        ``None``. Side effect: the root logger is reconfigured.
    """
    # Env-var fallback chain: explicit arg > env var > hard default.
    # ``.upper()`` normalises so users can write ``road_log_level=debug``.
    log_level = (level or os.getenv("ROAD_LOG_LEVEL", "INFO")).upper()
    # ``ROAD_LOG_FORMAT`` selects the renderer. Only ``text`` triggers the
    # plain format; any other value (including empty) keeps JSON.
    log_format = os.getenv("ROAD_LOG_FORMAT", "json").lower()

    # ``StreamHandler(sys.stdout)`` writes each record to stdout. In
    # containerised deployments stdout is captured by the runtime's log
    # driver — do not write to files from here, the orchestrator handles it.
    handler = logging.StreamHandler(sys.stdout)
    if log_format == "text":
        # Classic multi-field layout tuned for human reading during local dev.
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s  %(message)s")
        )
    else:
        handler.setFormatter(_JSONFormatter())

    # Root logger is the top of the hierarchy. Every named logger
    # (``logging.getLogger("road_safety.server")`` etc.) propagates here, so
    # configuring just the root is enough to catch everything.
    root = logging.getLogger()
    # Clear first — otherwise repeated calls stack handlers and produce
    # duplicate lines for every event.
    root.handlers.clear()
    root.addHandler(handler)
    # ``getattr(logging, log_level, logging.INFO)`` converts the string
    # level name to the numeric constant, defaulting to INFO on typos.
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Third-party library loggers we deliberately quiet down:
    #   - uvicorn.access: would log every single HTTP request, drowning out
    #     application logs and the SSE stream. WARNING keeps errors visible.
    #   - ultralytics: YOLOv8 emits noisy INFO-level per-inference chatter.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("ultralytics").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger for a caller module.

    Thin wrapper around ``logging.getLogger`` so callers can import one
    symbol from this module rather than pulling in the stdlib
    ``logging`` package for a single call. Every module that logs should
    pass ``__name__`` so the logger name matches the module path and shows
    up as the ``logger`` field in the JSON output.

    Args:
        name: Logger name, conventionally ``__name__`` at the call site.

    Returns:
        The (possibly newly-created, cached by the stdlib) logger instance.
    """
    return logging.getLogger(name)
