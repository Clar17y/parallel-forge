"""Bounded TypeSafe System One score transport for authorized Jev requests."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Any, TypeGuard

import httpx

from forge.application.ports.jev import JevProviderResponse
from forge.observability.redaction import Redactor

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class TypeSafeJevProvider:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = DEFAULT_ENDPOINT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if type(api_key) is not str or not api_key.strip():
            raise ValueError("TypeSafe API key is required")
        self._api_key = api_key.strip()
        self._redactor = Redactor(secrets=(self._api_key,))
        self._endpoint = endpoint
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None

    @classmethod
    def from_environment(
        cls, *, client: httpx.AsyncClient | None = None
    ) -> TypeSafeJevProvider | None:
        key = os.environ.get("TYPESAFE_API_KEY", "")
        return cls(api_key=key, client=client) if key.strip() else None

    async def evaluate(
        self,
        *,
        state: dict[str, Any],
        questions: dict[str, Any],
        model: str,
        timeout_seconds: float,
    ) -> JevProviderResponse:
        started = time.monotonic()
        body = bytearray()
        safe_state = self._redactor.redact(state)
        safe_questions = self._redactor.redact(questions)
        async with asyncio.timeout(timeout_seconds):
            async with self._client.stream(
                "POST",
                self._endpoint,
                json={"model": model, "state": safe_state, "questions": safe_questions},
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds)),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise ValueError("Jev provider returned a non-success status")
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_048_576:
                        raise ValueError("Jev provider response exceeded limit")
        try:
            parsed = json.loads(body)
        except ValueError, UnicodeError:
            raise ValueError("Jev provider response is invalid") from None
        if not isinstance(parsed, dict):
            raise TypeError("Jev provider response is invalid")
        raw_usage = parsed.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        answers = _score_answers(parsed.get("answers"), safe_questions)
        return JevProviderResponse(
            answers=answers,
            actual_model=self._safe_metadata(parsed.get("model")),
            input_units=_count(usage.get("input_tokens")),
            output_units=_count(usage.get("output_tokens")),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            request_id=self._safe_metadata(parsed.get("request_id")),
        )

    def _safe_metadata(self, value: object) -> str | None:
        if type(value) is not str or not 0 < len(value) <= 128:
            return None
        safe = self._redactor.redact(value)
        return safe if type(safe) is str and safe == value else None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["TypeSafeJevProvider"]


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2_147_483_647 else None


def _number(value: object, low: float, high: float) -> TypeGuard[int | float]:
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        return False
    return math.isfinite(value) and low <= value <= high


def _score_answers(raw: object, questions: object) -> dict[str, dict[str, float]]:
    if not isinstance(raw, dict) or not isinstance(questions, dict) or set(raw) != set(questions):
        return {}
    normalized: dict[str, dict[str, float]] = {}
    for key, answer in raw.items():
        question = questions[key]
        if (
            not isinstance(answer, dict)
            or not isinstance(question, dict)
            or answer.get("type") != "score"
        ):
            return {}
        criteria = question.get("criteria")
        if not isinstance(criteria, list):
            return {}
        levels = {str(index) for index in range(len(criteria))}
        legend, probabilities = answer.get("legend"), answer.get("probabilities")
        if (
            not isinstance(legend, dict)
            or not isinstance(probabilities, dict)
            or set(legend) != levels
            or set(probabilities) != levels
        ):
            return {}
        if any(
            type(legend[str(index)]) is not str or legend[str(index)] != criteria[index]
            for index in range(len(criteria))
        ):
            return {}
        if (
            any(not _number(value, 0, 1) for value in probabilities.values())
            or abs(sum(probabilities.values()) - 1) > 0.02
        ):
            return {}
        score, confidence = answer.get("score"), answer.get("confidence")
        if not _number(score, 0, len(criteria) - 1) or not _number(confidence, 0, 1):
            return {}
        normalized[key] = {"score": float(score), "confidence": float(confidence)}
    return normalized
