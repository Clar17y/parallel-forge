"""Bounded public event/metadata values, without private reasoning or pointers."""

from collections.abc import Mapping

from forge.observability.redaction import redact_value


def public_payload(value: Mapping[str, object]) -> dict[str, object]:
    def clean(item: object) -> object:
        if isinstance(item, dict):
            return {
                key: clean(child)
                for key, child in item.items()
                if not any(
                    part in key.lower().replace("_", "").replace("-", "")
                    for part in ("reasoning", "chainofthought", "storagepointer", "secret")
                )
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item

    result = clean(redact_value(dict(value)))
    return result if isinstance(result, dict) else {}
