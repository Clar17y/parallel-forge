"""Slice C domain: review prompt/parse contract."""

from forge.domain.direct import REVIEW_SCHEMA, build_direct_review_prompt, parse_review_result


def test_review_schema_shape() -> None:
    assert REVIEW_SCHEMA["type"] == "object"
    assert REVIEW_SCHEMA["required"] == ["verdict", "summary", "findings"]
    properties = REVIEW_SCHEMA["properties"]
    assert properties["verdict"]["enum"] == ["clean", "blocking"]
    item = properties["findings"]["items"]
    assert item["required"] == ["severity", "file", "detail"]
    assert item["properties"]["severity"]["enum"] == ["blocking", "note"]
    assert item["properties"]["file"]["type"] == ["string", "null"]


def test_review_prompt_marker_and_bounds() -> None:
    diff = "x" * 40000
    prompt = build_direct_review_prompt("Fix bug", diff, base_sha="a" * 40, head_sha="b" * 40)
    assert "REVIEW" in prompt
    assert "Fix bug" in prompt
    assert "read-only" in prompt.lower()
    assert "TRUNCATED" in prompt
    assert prompt.count("x") <= 32768 + 100


def test_parse_fenced_clean() -> None:
    r = parse_review_result('```json\n{"verdict":"clean","summary":"ok","findings":[]}\n```')
    assert r is not None and r.verdict == "clean" and r.degraded is False


def test_parse_blocking_bounded() -> None:
    import json
    findings = [{"severity": "blocking", "file": "a.py", "detail": "d" * 1500} for _ in range(35)]
    r = parse_review_result("```json\n" + json.dumps({"verdict": "blocking", "summary": "s", "findings": findings}) + "\n```")
    assert r is not None and len(r.findings) == 32
    assert len(r.findings[0].detail) == 1000


def test_parse_nonfenced_degraded() -> None:
    r = parse_review_result('{"verdict":"clean","summary":"ok","findings":[]}')
    assert r is not None and r.degraded is True


def test_parse_unparseable_none() -> None:
    assert parse_review_result("just prose, no json") is None
    assert parse_review_result('{"verdict":"maybe","summary":"s","findings":[]}') is None
