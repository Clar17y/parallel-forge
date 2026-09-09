"""Evaluation execution runtime resolving live gateways and observing controlled tool outcomes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from forge.agents.adk_gateway import AdkToolProvider, GoogleAdkGateway
from forge.agents.adk_runtime import AdkRuntime, AdkRuntimeProtocol
from forge.agents.prompt_loader import PromptLoader
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.provider_credentials import ProviderCredentialResolverPort
from forge.domain.tool import ToolCallStatus, ToolName
from forge.evaluations.check_evidence import read_check_evidence
from forge.evaluations.contracts import EvaluationCaseContract
from forge.evaluations.materializer import MaterializedFixture
from forge.observability.usage import PricingCatalog


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    """Observed outcomes from an agent execution in a materialized evaluation repository."""

    changed_paths: frozenset[str] = frozenset()
    test_results: Mapping[str, bool] = field(default_factory=lambda: MappingProxyType({}))
    check_results: Mapping[str, bool] = field(default_factory=lambda: MappingProxyType({}))
    assertion_results: Mapping[str, bool] = field(default_factory=lambda: MappingProxyType({}))
    denied_tool_calls: tuple[str, ...] = ()
    remediation_count: int = 0
    executed_tools: tuple[str, ...] = ()


class EvaluationToolObserver:
    """Track actual tool calls, enforce role boundaries, and record denied attempts."""

    def __init__(
        self,
        prohibited_tools: Sequence[str] = (),
        *,
        case: EvaluationCaseContract | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._case = case
        self._artifact_store = artifact_store
        self._reports: dict[str, tuple[dict[str, bool], dict[str, bool]]] = {}
        self.prohibited_tools: frozenset[str] = frozenset(prohibited_tools)
        self.denied_tool_calls: list[str] = []
        self.check_results: dict[str, bool] = {}
        self.remediation_count: int = 0
        self.executed_tools: list[str] = []
        self.changed_paths: set[str] = set()
        self.assertion_results: dict[str, bool] = {}
        self.test_results: dict[str, bool] = {}

    async def record_check_artifacts(
        self, result: Mapping[str, object], *, command_name: str
    ) -> None:
        if self._case is None or self._artifact_store is None:
            return
        # A later failed/missing report cannot retain credit from an earlier call.
        self.test_results.clear()
        self.assertion_results.clear()
        self._reports.pop(command_name, None)
        evidence = await read_check_evidence(
            self._case, self._artifact_store, result, command_name=command_name
        )
        if evidence is not None:
            self._reports[command_name] = evidence
        if set(self._reports) != set(self._case.required_checks):
            return
        for tests, assertions in self._reports.values():
            for target, values in (
                (self.test_results, tests),
                (self.assertion_results, assertions),
            ):
                for name, passed in values.items():
                    target[name] = target.get(name, True) and passed

    def record_tool_call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        allowed: bool = True,
    ) -> dict[str, object]:
        if not allowed or tool_name in self.prohibited_tools:
            self.denied_tool_calls.append(tool_name)
            return {"status": "error", "error": "tool_denied"}
        self.executed_tools.append(tool_name)
        return {"status": "ok"}

    def record_check_result(self, check_name: str, *, passed: bool) -> None:
        self.check_results[check_name] = passed
        if not passed:
            self.remediation_count += 1

    def record_test_result(self, test_name: str, *, passed: bool) -> None:
        """Record a test result decoded from controlled command evidence."""
        self.test_results[test_name] = passed

    def record_controlled_result(
        self,
        tool_name: ToolName,
        result: Mapping[str, object],
        *,
        arguments: Mapping[str, object] | None = None,
    ) -> None:
        """Record only the terminal evidence returned by a Forge-bound tool."""
        status = result.get("status")
        if status == ToolCallStatus.DENIED.value:
            self.denied_tool_calls.append(tool_name.value)
            return
        if status not in {
            ToolCallStatus.SUCCEEDED.value,
            ToolCallStatus.FAILED.value,
            ToolCallStatus.CANCELLED.value,
        }:
            return
        self.executed_tools.append(tool_name.value)
        metadata = result.get("metadata")
        if not isinstance(metadata, Mapping):
            return
        if tool_name is ToolName.GIT_DIFF:
            paths = metadata.get("changed_paths")
            if isinstance(paths, (list, tuple)) and all(isinstance(path, str) for path in paths):
                self.changed_paths.update(paths)
        elif tool_name is ToolName.BUILD_RUN_NAMED_CHECK:
            command_name = metadata.get("command_name")
            if not isinstance(command_name, str) and arguments is not None:
                command_name = arguments.get("command_name")
            if isinstance(command_name, str):
                self.record_check_result(
                    command_name, passed=status == ToolCallStatus.SUCCEEDED.value
                )


def observe_developer_execution(
    materialized: MaterializedFixture,
    case: EvaluationCaseContract,
    observer: EvaluationToolObserver | None = None,
) -> EvaluationObservation:
    """Inspect actual git diff, check, test, and assertion results from repository execution."""
    del materialized
    check_res: dict[str, bool] = dict(observer.check_results) if observer else {}
    test_res: dict[str, bool] = dict(observer.test_results) if observer else {}
    assertion_res: dict[str, bool] = dict(observer.assertion_results) if observer else {}
    remediation_count = observer.remediation_count if observer else 0
    denied = tuple(observer.denied_tool_calls) if observer else ()
    executed = tuple(observer.executed_tools) if observer else ()

    # Only a named check executed through the controlled runner may satisfy a
    # required check. Missing evidence is an observed failure, never an agent
    # declaration or a host-side subprocess substitute.
    for check in case.required_checks:
        check_res.setdefault(check, False)

    # Required tests and assertions require independently recorded controlled
    # evidence.  Evaluation contracts carry names, not arbitrary commands.
    for test in case.required_tests:
        test_res.setdefault(test, False)
    for assertion in case.required_assertions:
        assertion_res.setdefault(assertion, False)

    return EvaluationObservation(
        changed_paths=frozenset(observer.changed_paths) if observer else frozenset(),
        test_results=MappingProxyType(test_res),
        check_results=MappingProxyType(check_res),
        assertion_results=MappingProxyType(assertion_res),
        denied_tool_calls=denied,
        remediation_count=remediation_count,
        executed_tools=executed,
    )


def default_evaluation_pricing_catalog() -> PricingCatalog:
    return PricingCatalog.from_mapping(
        version="evaluation-pricing-unavailable-v1",
        entries={},
    )


def resolve_live_evaluation_gateway(
    credential_resolver: ProviderCredentialResolverPort,
    provider_reference: str,
    prompt_loader: PromptLoader,
    *,
    runtime: AdkRuntimeProtocol | None = None,
    pricing_catalog: PricingCatalog | None = None,
    currency: str = "USD",
    supported_provider: str = "google",
    tool_provider: AdkToolProvider | None = None,
) -> GoogleAdkGateway:
    """Resolve configured GoogleADK gateway behind Forge interfaces for live evaluation."""
    active_runtime = runtime or AdkRuntime(
        credential_resolver=credential_resolver,
        credential_reference=provider_reference,
    )
    active_pricing = pricing_catalog or default_evaluation_pricing_catalog()
    if tool_provider is None:
        raise ValueError("live evaluation requires a Forge-controlled tool provider")

    return GoogleAdkGateway(
        runtime=active_runtime,
        prompt_loader=prompt_loader,
        tool_provider=tool_provider,
        pricing_catalog=active_pricing,
        supported_provider=supported_provider,
        currency=currency,
    )


__all__ = [
    "EvaluationObservation",
    "EvaluationToolObserver",
    "observe_developer_execution",
    "resolve_live_evaluation_gateway",
]
