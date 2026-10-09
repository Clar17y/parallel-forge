"""Model families that accept an explicit reasoning effort."""

from __future__ import annotations

from typing import Final

BUDGET_REASONING_MODELS: Final[tuple[str, ...]] = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
)

LEVEL_REASONING_MODELS: Final[tuple[str, ...]] = (
    "gemini-3.5-flash",
    "gemini-3.8-flash",
    "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-customtools",
)

SUPPORTED_EFFORTS: Final[tuple[str, ...]] = ("low", "medium", "high")

RESERVED_CLI_EFFORTS: Final[frozenset[str]] = frozenset({
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "maximum",
    "ultra",
    "minimal",
    "none",
    "auto",
})


def parse_gemini_cli_composite_id(model: str) -> tuple[str, str] | None:
    """If model is a known Google Gemini CLI composite ID, return (base_model, effort_suffix)."""
    if not isinstance(model, str):
        return None
    prefix, sep, suffix = model.rpartition("-")
    if not sep or not prefix.startswith("gemini-"):
        return None
    if suffix.lower() in RESERVED_CLI_EFFORTS:
        return prefix, suffix
    return None


def gemini_cli_composite_error(model: str) -> str | None:
    """Return an error message if model is a known Gemini CLI composite identity."""
    composite = parse_gemini_cli_composite_id(model)
    if composite is None:
        return None
    base_model, _ = composite
    return (
        f"Model {model!r} is a CLI composite identity; use base API model "
        f"{base_model!r} with separately selected reasoning instead"
    )


def reasoning_model_family(provider: str, model: str) -> tuple[str, tuple[str, ...]] | None:
    """Return the exact supported model family and efforts; None means unsupported."""
    if provider != "google":
        return None
    if model in BUDGET_REASONING_MODELS:
        return "gemini_2_5", SUPPORTED_EFFORTS
    if model in LEVEL_REASONING_MODELS:
        return "gemini_3_levels", SUPPORTED_EFFORTS
    return None


def reasoning_effort_error(provider: str, model: str, effort: str) -> str | None:
    cli_error = gemini_cli_composite_error(model)
    if cli_error is not None:
        return cli_error
    support = reasoning_model_family(provider, model)
    if support is None:
        return f"Reasoning effort is not supported for provider/model {provider!r}/{model!r}"
    efforts = support[1]
    if effort not in efforts:
        return f"Reasoning effort {effort!r} is not supported for {model!r}; supported choices: {', '.join(efforts)}"
    return None
