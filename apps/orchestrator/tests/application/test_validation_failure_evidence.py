"""Bounded failure detail extracted from persisted controller check output."""

import json

from forge.application.services.validation_failure_evidence import (
    MAX_EXCERPT_CHARS_PER_CHECK,
    MAX_FAILED_NODES_PER_CHECK,
    MAX_FAILURE_DETAIL_CHARS,
    build_failure_detail,
    extract_check_failure,
)


def _envelope(text: str, stream: str = "stdout") -> bytes:
    return json.dumps(
        {
            "captured_byte_count": len(text.encode()),
            "encoding": "utf-8-replacement",
            "original_byte_count": len(text.encode()),
            "stream": stream,
            "text": text,
            "truncated": False,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


PYTEST_OUTPUT = (
    "....F\n"
    "FAILED tests/test_alpha.py::test_one - AssertionError: boom\n"
    "FAILED tests/test_beta.py::test_two - ValueError\n"
    "FAILED tests/test_alpha.py::test_one - AssertionError: boom\n"
    "== 2 failed, 6251 passed in 12.34s ==\n"
)


def test_extracts_failed_nodes_counts_and_excerpt_from_envelope():
    evidence = extract_check_failure("unit", _envelope(PYTEST_OUTPUT))
    assert evidence.check_name == "unit"
    assert evidence.failing_nodes == (
        "tests/test_alpha.py::test_one",
        "tests/test_beta.py::test_two",
    )
    assert evidence.counts == "2 failed, 6251 passed"
    assert "2 failed, 6251 passed" in evidence.excerpt


def test_raw_text_output_is_accepted_without_envelope():
    evidence = extract_check_failure("unit", PYTEST_OUTPUT.encode())
    assert evidence.failing_nodes[0] == "tests/test_alpha.py::test_one"
    assert evidence.counts == "2 failed, 6251 passed"


def test_no_parseable_nodes_degrades_to_name_and_tail_excerpt():
    evidence = extract_check_failure("lint", _envelope("some opaque failure\nlast line\n"))
    assert evidence.failing_nodes == ()
    assert evidence.counts is None
    assert evidence.excerpt.endswith("last line")
    detail = build_failure_detail([evidence])
    assert "lint" in detail
    assert "last line" in detail


def test_empty_and_malformed_output_never_raise():
    for blob in (b"", b"\x00\xff\xfe binary \x01\x02", b"{not json", b'{"text": 3}'):
        evidence = extract_check_failure("unit", blob)
        assert evidence.check_name == "unit"
        assert len(evidence.excerpt) <= MAX_EXCERPT_CHARS_PER_CHECK


def test_node_list_and_excerpt_are_truncated_deterministically():
    text = "".join(f"FAILED tests/test_m.py::test_{index} - boom\n" for index in range(50))
    text += "x" * 100_000
    evidence = extract_check_failure("unit", text.encode())
    assert len(evidence.failing_nodes) == MAX_FAILED_NODES_PER_CHECK
    assert evidence.failing_nodes[0] == "tests/test_m.py::test_0"
    assert evidence.extra_node_count == 50 - MAX_FAILED_NODES_PER_CHECK
    assert len(evidence.excerpt) <= MAX_EXCERPT_CHARS_PER_CHECK
    assert evidence.excerpt.endswith("x" * 20)


def test_total_detail_is_capped_across_many_failed_checks():
    evidences = [
        extract_check_failure(
            f"check-{index}", _envelope(PYTEST_OUTPUT + "y" * 5000)
        )
        for index in range(20)
    ]
    detail = build_failure_detail(evidences)
    assert len(detail) <= MAX_FAILURE_DETAIL_CHARS
    assert "check-0" in detail
    assert "tests/test_alpha.py::test_one" in detail


def test_detail_is_deterministic_from_same_inputs():
    first = build_failure_detail([extract_check_failure("unit", _envelope(PYTEST_OUTPUT))])
    second = build_failure_detail([extract_check_failure("unit", _envelope(PYTEST_OUTPUT))])
    assert first == second
    assert "unit" in first
