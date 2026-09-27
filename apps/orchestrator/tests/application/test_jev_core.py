import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from forge.application.ports.jev import JevRequest
from forge.application.services.jev import JevService
from forge.application.services.jev_review import REVIEW_QUESTIONS
from forge.domain.policy import JevPolicy
from forge.ranking.jev import TypeSafeJevProvider


@pytest.mark.asyncio
async def test_provider_redacts_known_credential_and_rejects_redirect():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/collect"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        provider = TypeSafeJevProvider(api_key="test-secret", client=client)
        with pytest.raises(ValueError, match="non-success"):
            await provider.evaluate(
                state={"text": "test-secret"},
                questions={
                    "m0": {
                        "type": "score",
                        "instructions": "test-secret",
                        "criteria": ["no", "yes"],
                    }
                },
                model="any-future-model",
                timeout_seconds=2,
            )
    assert len(captured) == 1
    assert b"test-secret" not in captured[0].content
    assert captured[0].headers["Authorization"] == "Bearer test-secret"


@pytest.mark.asyncio
async def test_provider_discards_credential_in_response_provenance():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "test-secret",
                "request_id": "prefix-test-secret",
                "answers": {
                    "m0": {
                        "type": "score",
                        "score": 1,
                        "legend": {"0": "no", "1": "yes"},
                        "probabilities": {"0": 0, "1": 1},
                        "confidence": 1,
                    }
                },
                "usage": {"input_tokens": 8, "output_tokens": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await TypeSafeJevProvider(api_key="test-secret", client=client).evaluate(
            state={},
            questions={"m0": {"type": "score", "instructions": "x", "criteria": ["no", "yes"]}},
            model="free-alias",
            timeout_seconds=2,
        )
    assert result.actual_model is None
    assert result.request_id is None
    assert result.answers == {"m0": {"score": 1.0, "confidence": 1.0}}


@pytest.mark.parametrize("max_candidates", [1, 96])
async def test_actual_review_questions_survive_service_and_transport_redaction(max_candidates):
    captured = []
    active = False
    work = SimpleNamespace(
        jev=SimpleNamespace(reserve=AsyncMock(return_value=None), settle=AsyncMock(),
                            observe_unavailable=AsyncMock()),
        commit=AsyncMock(),
    )

    @asynccontextmanager
    async def factory():
        nonlocal active
        active = True
        try:
            yield work
        finally:
            active = False

    def handler(request):
        assert not active
        payload = json.loads(request.content)
        captured.append(payload)
        assert len(payload["questions"]) == 5
        assert all(isinstance(question, dict) for question in payload["questions"].values())
        return httpx.Response(200, json={
            "model": payload["model"],
            "answers": {
                key: {"type": "score", "score": 1, "confidence": 0.8,
                      "legend": {str(i): label for i, label in enumerate(question["criteria"])},
                      "probabilities": {str(i): int(i == 1) for i in range(len(question["criteria"]))}}
                for key, question in payload["questions"].items()
            },
            "usage": {"input_tokens": 100, "output_tokens": 10},
        })

    policy = JevPolicy(mode="on", allow_remote=True, model="future-model-alias",
                       max_candidates=max_candidates)
    request = JevRequest(run_id=uuid4(), policy_version=1, operation_key="review",
                         kind="review_focus", worktree_digest="a" * 64,
                         state={"hunks": [{"path": "source.py", "diff": "+value = 1"}]},
                         questions=REVIEW_QUESTIONS)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = JevService(factory, TypeSafeJevProvider(api_key="test-key", client=client))
        result = await service.evaluate(request, policy=policy)

    assert result.status == "succeeded"
    assert len(captured) == 1
    assert result.actual_model == "future-model-alias"
    assert set(result.answers) == set(REVIEW_QUESTIONS)
    assert len(result.answers) == 5
    work.jev.settle.assert_awaited_once()
