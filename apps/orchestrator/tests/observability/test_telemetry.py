"""Deterministic telemetry behavior tests."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from forge.observability.context import CorrelationContext
from forge.observability.telemetry import Telemetry
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


class Clock:
    def __init__(self) -> None:
        self.values = iter((10.0, 10.125))

    def __call__(self) -> float:
        return next(self.values)


def test_span_emits_bounded_structured_success_record() -> None:
    records: list[str] = []
    telemetry = Telemetry(sink=records.append, clock=Clock())
    run_id = uuid4()

    with telemetry.start_span(
        "provider.call",
        retry_number=2,
        attributes={"safe": "value", "token": "Bearer should-not-log"},
        context=CorrelationContext(run_id=run_id),
    ):
        pass

    record = json.loads(records[0])
    assert record["status"] == "ok"
    assert record["retry_number"] == 2
    assert record["duration_ms"] == 125
    assert record["correlation"] == {"run_id": str(run_id)}
    assert record["attributes"]["token"] == "[REDACTED]"


def test_span_emits_failure_without_serializing_exception_or_secret() -> None:
    records: list[str] = []
    telemetry = Telemetry(sink=records.append, clock=Clock())

    with pytest.raises(RuntimeError, match="remote failure"), telemetry.start_span("provider.call"):
        raise RuntimeError("remote failure Bearer abcdefghijkl")

    record = json.loads(records[0])
    assert record["status"] == "error"
    assert "remote failure" in record["error"]
    assert "abcdefghijkl" not in records[0]


def test_span_records_correlation_retry_duration_and_safe_attributes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    run_id = uuid4()
    telemetry = Telemetry(
        tracer=provider.get_tracer("forge-test"),
        sink=lambda _record: None,
        clock=Clock(),
    )

    with telemetry.start_span(
        "provider.call",
        retry_number=3,
        attributes={"authorization": "Bearer abcdefghijkl"},
        context=CorrelationContext(run_id=run_id),
    ):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code is StatusCode.OK
    assert span.attributes is not None
    assert span.attributes["forge.run_id"] == str(run_id)
    assert span.attributes["forge.retry_number"] == 3
    assert span.attributes["forge.duration_ms"] == 125
    assert "abcdefghijkl" not in str(span.attributes)
    provider.shutdown()
