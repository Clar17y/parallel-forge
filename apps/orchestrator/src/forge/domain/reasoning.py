"""Model families that accept an explicit reasoning effort."""

from __future__ import annotations

import re
from typing import Final

_MODEL_FAMILIES: Final[tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...]] = (
    ("gemini_2_5", re.compile(r"\Agemini-2\.5-(?:flash|pro)(?:-[a-z0-9.]+)?\Z"), ("low", "medium", "high")),
    ("gemini_3_levels", re.compile(r"\Agemini-(?:3\.[58]-flash|3\.1-pro)(?:-[a-z0-9.]+)?\Z"), ("low", "medium", "high")),
    ("gemini_3_pro", re.compile(r"\Agemini-3(?:\.0)?-pro(?:-[a-z0-9.]+)?\Z"), ("low", "high")),
)


def reasoning_model_family(provider: str, model: str) -> tuple[str, tuple[str, ...]] | None:
    """Return the exact supported model family and efforts; None means unsupported."""
    if provider != "google":
        return None
    for family, pattern, efforts in _MODEL_FAMILIES:
        if pattern.fullmatch(model):
            return family, efforts
    return None


def reasoning_effort_error(provider: str, model: str, effort: str) -> str | None:
    support = reasoning_model_family(provider, model)
    if support is None:
        return f"Reasoning effort is not supported for provider/model {provider!r}/{model!r}"
    efforts = support[1]
    if effort not in efforts:
        return f"Reasoning effort {effort!r} is not supported for {model!r}; supported choices: {', '.join(efforts)}"
    return None
