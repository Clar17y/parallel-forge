"""Bounded Jev evaluation with durable admission before source egress."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from typing import Any

from forge.application.ports.jev import JevProvider, JevProviderResponse, JevRequest, JevResult
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.policy import JevPolicy
from forge.observability.redaction import Redactor

_QUESTION_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_MAX_REQUEST_BYTES = 250_000
_MAX_SCORE_RESULT_CHARS = 100_000


class JevService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        provider: JevProvider | None,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = provider
        self._redactor = redactor or Redactor()

    async def evaluate(self, request: JevRequest, *, policy: JevPolicy) -> JevResult:
        if not isinstance(request, JevRequest) or not isinstance(policy, JevPolicy):
            raise TypeError("Jev requires a typed request and policy")
        if policy.mode == "off":
            return JevResult(status="off", requested_model=policy.model)
        if request.kind == "semantic_search" and not policy.semantic_search:
            return JevResult(status="off", requested_model=policy.model)
        if request.kind == "review_focus" and not policy.review_focus:
            return JevResult(status="off", requested_model=policy.model)
        safe_state = self._redactor.redact(request.state)
        safe_questions = self._redactor.redact(request.questions)
        if not isinstance(safe_state, dict) or not isinstance(safe_questions, dict):
            return await self._unavailable(request, policy, "invalid_request")
        try:
            encoded = json.dumps(
                {"model": policy.model, "state": safe_state, "questions": safe_questions},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        except TypeError, ValueError, OverflowError:
            return await self._unavailable(request, policy, "invalid_request")
        digest = hashlib.sha256(encoded).hexdigest()
        # Review asks five fixed risk questions over a separately bounded source set.
        question_limit = 5 if request.kind == "review_focus" else policy.max_candidates
        if len(request.questions) > question_limit:
            return await self._unavailable(request, policy, "candidate_limit", digest=digest)
        if not self._valid_questions(safe_questions):
            return await self._unavailable(request, policy, "invalid_questions", digest=digest)
        if len(encoded) > _MAX_REQUEST_BYTES:
            return await self._unavailable(request, policy, "request_limit", digest=digest)
        # UTF-8 bytes conservatively reserve at least one input unit per byte.
        units = len(encoded)
        if not policy.allow_remote or self._provider is None:
            return await self._unavailable(
                request, policy, "remote_not_enabled_or_unavailable", digest=digest
            )
        async with self._uow_factory() as work:
            prior = await work.jev.reserve(
                request,
                request_digest=digest,
                model=policy.model,
                input_units=units,
                max_requests=policy.max_requests_per_run,
                max_input_units=policy.max_input_units_per_run,
                mode=policy.mode,
                policy=policy,
            )
            await work.commit()
        if prior is not None:
            return prior
        response: JevResult
        cancelled = False
        started = time.monotonic()
        try:
            async with asyncio.timeout(policy.timeout_seconds):
                raw = await self._provider.evaluate(
                    state=safe_state,
                    questions=safe_questions,
                    model=policy.model,
                    timeout_seconds=policy.timeout_seconds,
                )
            try:
                response = self._validated_response(raw, safe_questions, request, policy)
            except TypeError, ValueError, OverflowError:
                response = self._unknown_with_metadata(raw, policy)
        except asyncio.CancelledError:
            cancelled = True
            response = JevResult(
                status="unknown",
                requested_model=policy.model,
                diagnostic="cancelled_after_admission",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        except Exception:  # noqa: BLE001 - provider errors cannot expose payloads or credentials
            response = JevResult(
                status="unknown",
                requested_model=policy.model,
                diagnostic="provider_failure_after_admission",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        async with self._uow_factory() as work:
            await work.jev.settle(request, response)
            await work.commit()
        if cancelled:
            raise asyncio.CancelledError
        return response

    async def _unavailable(
        self, request: JevRequest, policy: JevPolicy, diagnostic: str, *, digest: str | None = None
    ) -> JevResult:
        # Invalid structures have no serializable source digest. The operation and
        # source-scope identities still bind their zero-cost observation.
        request_digest = digest or hashlib.sha256(diagnostic.encode("utf-8")).hexdigest()
        async with self._uow_factory() as work:
            await work.jev.observe_unavailable(
                request, request_digest=request_digest, policy=policy, diagnostic=diagnostic
            )
            await work.commit()
        return JevResult(status="unavailable", requested_model=policy.model, diagnostic=diagnostic)

    @staticmethod
    def _valid_questions(questions: dict[str, Any]) -> bool:
        for key, value in questions.items():
            if (
                type(key) is not str
                or not _QUESTION_ID.fullmatch(key)
                or not isinstance(value, dict)
            ):
                return False
            if value.get("type") != "score" or not isinstance(value.get("instructions"), str):
                return False
            criteria = value.get("criteria")
            if (
                not isinstance(criteria, list)
                or not 2 <= len(criteria) <= 10
                or any(type(item) is not str for item in criteria)
            ):
                return False
        return True

    def _validated_response(
        self,
        raw: JevProviderResponse,
        questions: dict[str, Any],
        request: JevRequest,
        policy: JevPolicy,
    ) -> JevResult:
        if not isinstance(raw, JevProviderResponse) or not isinstance(raw.answers, dict):
            raise TypeError("invalid provider response")
        if set(raw.answers) != set(questions):
            raise ValueError("incomplete provider scores")
        answers: dict[str, dict[str, float | int]] = {}
        for key, value in raw.answers.items():
            if not isinstance(value, dict) or set(value) != {"score", "confidence"}:
                raise ValueError("invalid provider score")
            score, confidence = value["score"], value["confidence"]
            if (
                type(score) not in (float, int)
                or not math.isfinite(score)
                or not 0 <= score <= len(questions[key]["criteria"]) - 1
            ):
                raise ValueError("provider score outside criteria")
            if (
                type(confidence) not in (float, int)
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                raise ValueError("provider confidence outside range")
            answers[key] = {"score": float(score), "confidence": float(confidence)}
        input_units, output_units, duration_ms = (
            self._required_count(unit)
            for unit in (raw.input_units, raw.output_units, raw.duration_ms)
        )
        if len(json.dumps(answers)) > _MAX_SCORE_RESULT_CHARS:
            raise ValueError("provider result exceeds limit")
        return JevResult(
            status="ranked" if request.kind == "search_ranking" else "succeeded",
            answers=answers,
            requested_model=policy.model,
            actual_model=self._safe_metadata(raw.actual_model),
            input_units=input_units,
            output_units=output_units,
            duration_ms=duration_ms,
            request_id=self._safe_metadata(raw.request_id),
        )

    def _unknown_with_metadata(self, raw: object, policy: JevPolicy) -> JevResult:
        if not isinstance(raw, JevProviderResponse):
            return JevResult(
                status="unknown",
                requested_model=policy.model,
                diagnostic="invalid_provider_response",
            )
        return JevResult(
            status="unknown",
            requested_model=policy.model,
            actual_model=self._safe_metadata(raw.actual_model),
            input_units=self._safe_count(raw.input_units),
            output_units=self._safe_count(raw.output_units),
            duration_ms=self._safe_count(raw.duration_ms),
            request_id=self._safe_metadata(raw.request_id),
            diagnostic="invalid_provider_scores",
        )

    def _safe_metadata(self, value: object) -> str | None:
        if type(value) is not str or not 0 < len(value) <= 128:
            return None
        safe = self._redactor.redact(value)
        return safe if type(safe) is str and safe == value else None

    @staticmethod
    def _required_count(value: object) -> int:
        if type(value) is not int or not 0 <= value <= 2_147_483_647:
            raise ValueError("invalid provider usage")
        return value

    @staticmethod
    def _safe_count(value: object) -> int:
        return value if type(value) is int and 0 <= value <= 2_147_483_647 else 0


__all__ = ["JevService"]
