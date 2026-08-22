"""OpenTelemetry-compatible spans with bounded structured completion logs."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from forge.observability.context import CorrelationContext, current_context
from forge.observability.redaction import Redactor

_LOGGER = logging.getLogger("forge.telemetry")


class Telemetry:
    """Create correlated spans without requiring an external collector."""

    def __init__(
        self,
        *,
        tracer: Tracer | None = None,
        sink: Callable[[str], None] | None = None,
        redactor: Redactor | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tracer = tracer or trace.get_tracer("forge")
        self._sink = sink or _LOGGER.info
        self._redactor = redactor or Redactor()
        self._clock = clock

    @contextmanager
    def start_span(
        self,
        operation: str,
        *,
        retry_number: int = 0,
        attributes: Mapping[str, object] | None = None,
        context: CorrelationContext | None = None,
    ) -> Iterator[Span]:
        """Emit one safe span/log record and preserve the caller's exception."""

        if not isinstance(operation, str) or not operation or len(operation) > 255:
            raise ValueError("operation must contain 1-255 characters")
        if type(retry_number) is not int or retry_number < 0:
            raise ValueError("retry number must be a nonnegative integer")
        correlation = context or current_context()
        safe_operation = self._redactor.redact(operation)
        if not isinstance(safe_operation, str):
            raise TypeError("redacted operation name must remain a string")
        safe_attributes = self._redactor.redact(dict(attributes or {}))
        correlation_values = correlation.to_dict()
        started = self._clock()
        record: dict[str, Any] = {
            "operation": safe_operation,
            "retry_number": retry_number,
            "correlation": correlation_values,
            "attributes": safe_attributes,
        }

        with self._tracer.start_as_current_span(
            safe_operation,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("forge.operation", safe_operation)
            span.set_attribute("forge.retry_number", retry_number)
            for name, value in correlation_values.items():
                span.set_attribute(f"forge.{name}", value)
            attributes_json = json.dumps(safe_attributes, sort_keys=True, separators=(",", ":"))
            bounded_attributes = self._redactor.redact(attributes_json)
            if isinstance(bounded_attributes, str):
                span.set_attribute("forge.attributes_json", bounded_attributes)
            try:
                yield span
            except BaseException as error:
                safe_error = self._safe_error(error)
                record["status"] = "error"
                record["error"] = safe_error
                span.set_attribute("forge.status", "error")
                span.set_attribute("forge.error", safe_error)
                span.set_status(Status(StatusCode.ERROR, safe_error))
                raise
            else:
                record["status"] = "ok"
                span.set_attribute("forge.status", "ok")
                span.set_status(Status(StatusCode.OK))
            finally:
                duration_ms = max(0, round((self._clock() - started) * 1000))
                record["duration_ms"] = duration_ms
                span.set_attribute("forge.duration_ms", duration_ms)
                self._emit(record)

    def _safe_error(self, error: BaseException) -> str:
        value = self._redactor.redact(str(error))
        return value if isinstance(value, str) else "[UNSUPPORTED]"

    def _emit(self, record: Mapping[str, object]) -> None:
        safe_record = self._redactor.redact(record)
        rendered = json.dumps(safe_record, sort_keys=True, separators=(",", ":"))
        with suppress(Exception):
            self._sink(rendered)


__all__ = ["Telemetry"]
