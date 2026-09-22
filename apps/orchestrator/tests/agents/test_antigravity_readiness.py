"""Fail-closed readiness for the supported Antigravity successor client."""

from pathlib import Path

import pytest
from forge.agents.antigravity_readiness import (
    ANTIGRAVITY_CLIENT_VERSION,
    ANTIGRAVITY_ISOLATION_AGENT,
    ANTIGRAVITY_TOOL_BOUNDARY_WARNING,
    AntigravityInitObservation,
    AntigravityPreflightFailure,
    AntigravityPreflightWarning,
    AntigravityReadinessGate,
    AntigravityReadinessVerifier,
)
from forge.agents.gemini_gateway import GeminiCapabilityReport, GeminiInstallation
from forge.domain.capability_evidence import CapabilityEvidenceScope
from forge.domain.subscription import ReasoningEffort, RouteSpec, SpecialistPurpose
from forge.domain.tool import ToolName

_EXECUTABLE_DIGEST = "162607893eaacaf7b4a34bcd0bc3978342c6707b0340f96040f0139ac904dd22"
_LEAKED_TOOLS = (
    "ask_custom_permission",
    "ask_permission",
    "ask_question",
    "browser_click_element",
    "browser_drag_pixel_to_pixel",
    "browser_get_dom",
    "browser_get_network_request",
    "browser_input",
    "browser_list_network_requests",
    "browser_mouse_down",
    "browser_mouse_up",
    "browser_move_mouse",
    "browser_press_key",
    "browser_refresh_page",
    "browser_resize_window",
    "browser_scroll",
    "browser_scroll_dom",
    "browser_select_option",
    "browser_subagent",
    "call_mcp_tool",
    "capture_browser_console_logs",
    "capture_browser_screenshot",
    "click_browser_pixel",
    "command_status",
    "define_subagent",
    "delete_knowledge",
    "execute_browser_javascript",
    "find_by_name",
    "finish",
    "generate_image",
    "grep_search",
    "invoke_subagent",
    "list_browser_pages",
    "list_dir",
    "list_permissions",
    "list_resources",
    "manage_inbox",
    "manage_subagents",
    "manage_task",
    "multi_replace_file_content",
    "notebook_edit",
    "notebook_execution",
    "open_browser_url",
    "read_browser_page",
    "read_resource",
    "read_url_content",
    "replace_file_content",
    "run_command",
    "schedule",
    "search_web",
    "sed_file",
    "send_command_input",
    "send_message",
    "view_file",
    "wait",
    "wait_5_seconds",
    "write_to_file",
)


def _observation(**changes: object) -> AntigravityInitObservation:
    values: dict[str, object] = {
        "client_version": ANTIGRAVITY_CLIENT_VERSION,
        "executable_digest": _EXECUTABLE_DIGEST,
        "selected_agent": ANTIGRAVITY_ISOLATION_AGENT,
        "agent_selection_confirmed": True,
        "declared_tools": (),
        "observed_tools": _LEAKED_TOOLS,
        "prompt_count": 0,
        "provider_turn_count": 0,
    }
    values.update(changes)
    return AntigravityInitObservation(**values)  # type: ignore[arg-type]


def test_current_1_2_7_zero_turn_inventory_warns_but_allows_conformance() -> None:
    assert ANTIGRAVITY_CLIENT_VERSION == "1.2.7"

    report = AntigravityReadinessGate(_EXECUTABLE_DIGEST).assess(
        _observation(agent_selection_confirmed=False)
    )

    assert len(_LEAKED_TOOLS) == 57
    assert report.preflight_failures == ()
    assert report.preflight_warnings == (
        AntigravityPreflightWarning.APPROVED_TOOLS_UNPROVED,
        AntigravityPreflightWarning.AGENT_SELECTION_UNCONFIRMED,
    )
    assert report.native_tool_extras == _LEAKED_TOOLS
    assert report.ready_for_live_conformance
    assert report.tool_boundary_warning_required
    assert not report.runtime_admissible
    assert "cannot guarantee" in ANTIGRAVITY_TOOL_BOUNDARY_WARNING


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"client_version": "1.2.3"}, AntigravityPreflightFailure.CLIENT_VERSION),
        ({"executable_digest": "a" * 64}, AntigravityPreflightFailure.EXECUTABLE_IDENTITY),
        ({"selected_agent": "default"}, AntigravityPreflightFailure.AGENT_SELECTION),
        ({"declared_tools": ("view_file",)}, AntigravityPreflightFailure.DECLARED_NATIVE_TOOLS),
        (
            {"prompt_count": 1, "observed_tools": ()},
            AntigravityPreflightFailure.ZERO_TURN_VIOLATION,
        ),
        (
            {"provider_turn_count": 1, "observed_tools": ()},
            AntigravityPreflightFailure.ZERO_TURN_VIOLATION,
        ),
    ],
)
def test_preflight_identity_and_zero_turn_drift_fail_closed(changes, failure) -> None:
    report = AntigravityReadinessGate(_EXECUTABLE_DIGEST).assess(_observation(**changes))

    assert failure in report.preflight_failures
    assert not report.ready_for_live_conformance
    assert not report.runtime_admissible


def test_clean_zero_turn_is_only_ready_for_live_proof_not_runtime() -> None:
    report = AntigravityReadinessGate(_EXECUTABLE_DIGEST).assess(_observation(observed_tools=()))

    assert report.preflight_failures == ()
    assert report.preflight_warnings == (AntigravityPreflightWarning.APPROVED_TOOLS_UNPROVED,)
    assert report.ready_for_live_conformance
    assert report.tool_boundary_warning_required
    assert not report.runtime_admissible


def test_unconfirmed_selection_without_observed_tools_is_an_explicit_warning() -> None:
    report = AntigravityReadinessGate(_EXECUTABLE_DIGEST).assess(
        _observation(agent_selection_confirmed=False, observed_tools=())
    )

    assert report.preflight_failures == ()
    assert report.preflight_warnings == (
        AntigravityPreflightWarning.APPROVED_TOOLS_UNPROVED,
        AntigravityPreflightWarning.AGENT_SELECTION_UNCONFIRMED,
    )
    assert report.ready_for_live_conformance
    assert report.tool_boundary_warning_required


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_tools": ("view_file", "view_file")},
        {"observed_tools": ("Write File",)},
        {"prompt_count": True},
        {"provider_turn_count": -1},
        {"agent_selection_confirmed": 1},
    ],
)
def test_observation_rejects_noncanonical_or_invalid_probe_data(changes) -> None:
    with pytest.raises(ValueError):
        _observation(**changes)


@pytest.mark.asyncio
async def test_readiness_verifier_cannot_admit_runtime_without_live_evidence(tmp_path) -> None:
    report = AntigravityReadinessGate(_EXECUTABLE_DIGEST).assess(_observation(observed_tools=()))
    verifier = AntigravityReadinessVerifier(report)
    installation = GeminiInstallation(
        executable=str(Path(__file__).resolve()),
        cwd=str(tmp_path),
        home=str(tmp_path),
        model="gemini-3.8-flash",
        effort="medium",
        account="test-account",
        executable_digest="b" * 64,
    )
    scope = CapabilityEvidenceScope(
        route=RouteSpec(
            provider="google",
            client="agy",
            model="gemini-3.8-flash",
            effort=ReasoningEffort.MEDIUM,
        ),
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        tool_surface=tuple(
            sorted(
                {
                    ToolName.REPOSITORY_READ_FILE,
                    ToolName.REPOSITORY_WRITE_FILE,
                    ToolName.BUILD_RUN_NAMED_CHECK,
                },
                key=lambda item: item.value,
            )
        ),
    )

    result = await verifier.verify(installation, scope)

    assert result == GeminiCapabilityReport()
    assert not result.admits(installation, scope)
