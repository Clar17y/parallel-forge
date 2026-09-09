from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from forge.domain.agent import ReviewDecision, ReviewOutput
from forge.domain.evidence import (
    EvidenceManifest,
    EvidenceManifestError,
    EvidenceStatus,
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    ValidationEvidenceMember,
    ValidationStatus,
    decode_evidence_manifest,
    encode_evidence_manifest,
    evidence_manifest_digest,
)
from forge.domain.review import FindingSeverity, ReviewFinding
from pydantic import ValidationError

_NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")
_FIXED_UUID_1 = UUID("11111111-1111-1111-1111-111111111111")
_FIXED_UUID_2 = UUID("22222222-2222-2222-2222-222222222222")
_FIXED_UUID_3 = UUID("33333333-3333-3333-3333-333333333333")
_FIXED_UUID_4 = UUID("44444444-4444-4444-4444-444444444444")
_FIXED_UUID_5 = UUID("55555555-5555-5555-5555-555555555555")

_SAMPLE_STARTED = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
_SAMPLE_COMPLETED = datetime(2026, 9, 6, 12, 5, 0, tzinfo=UTC)
_SAMPLE_RESOLVED = datetime(2026, 9, 6, 12, 10, 0, tzinfo=UTC)


def make_member(
    result_id: UUID | None = None,
    check_name: str = "pytest",
    command_name: str = "run_tests",
    command_version: int = 1,
    command_digest: str = "a" * 64,
    command_result_digest: str = "b" * 64,
    stdout_digest: str = "c" * 64,
    stderr_digest: str = "d" * 64,
    status: EvidenceStatus = EvidenceStatus.PASSED,
    exit_code: int | None = 0,
    started_at: datetime = _SAMPLE_STARTED,
    completed_at: datetime = _SAMPLE_COMPLETED,
) -> ValidationEvidenceMember:
    return ValidationEvidenceMember(
        result_id=result_id or uuid4(),
        check_name=check_name,
        command_name=command_name,
        command_version=command_version,
        command_digest=command_digest,
        command_result_digest=command_result_digest,
        stdout_digest=stdout_digest,
        stderr_digest=stderr_digest,
        status=status,
        exit_code=exit_code,
        started_at=started_at,
        completed_at=completed_at,
    )


def make_validation_manifest(
    evidence_set_id: UUID = _FIXED_UUID_1,
    run_id: UUID = _FIXED_UUID_2,
    step_id: UUID = _FIXED_UUID_3,
    policy_version: int = 1,
    head_sha: str = "e" * 40,
    prior_review_evidence_set_id: UUID | None = None,
    members: tuple[ValidationEvidenceMember, ...] = (),
) -> ValidationEvidenceManifest:
    return ValidationEvidenceManifest(
        evidence_set_id=evidence_set_id,
        run_id=run_id,
        step_id=step_id,
        policy_version=policy_version,
        head_sha=head_sha,
        prior_review_evidence_set_id=prior_review_evidence_set_id,
        members=members,
    )


def make_review_manifest(
    evidence_set_id: UUID = _FIXED_UUID_1,
    run_id: UUID = _FIXED_UUID_2,
    step_id: UUID = _FIXED_UUID_3,
    policy_version: int = 1,
    head_sha: str = "e" * 40,
    producer_execution_id: UUID = _FIXED_UUID_4,
    validation_evidence_set_id: UUID = _FIXED_UUID_5,
    review: ReviewOutput | None = None,
) -> ReviewEvidenceManifest:
    if review is None:
        review = ReviewOutput(
            decision=ReviewDecision.APPROVE,
            findings=(),
            tested_claims=("tests pass",),
            missing_evidence=(),
            summary="All checks verified successfully.",
        )
    return ReviewEvidenceManifest(
        evidence_set_id=evidence_set_id,
        run_id=run_id,
        step_id=step_id,
        policy_version=policy_version,
        head_sha=head_sha,
        producer_execution_id=producer_execution_id,
        validation_evidence_set_id=validation_evidence_set_id,
        review=review,
    )


# ---------------------------------------------------------------------------
# ValidationEvidenceMember unit tests
# ---------------------------------------------------------------------------


def test_validation_member_valid() -> None:
    member = make_member()
    assert member.status is EvidenceStatus.PASSED
    assert member.exit_code == 0
    assert member.check_name == "pytest"
    assert ValidationStatus is EvidenceStatus


def test_validation_member_status_and_exit_code_rules() -> None:
    # PASSED requires exit_code == 0
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(status=EvidenceStatus.PASSED, exit_code=1)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(status=EvidenceStatus.PASSED, exit_code=None)

    # FAILED must not claim exit_code 0 (None is allowed for timeout)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(status=EvidenceStatus.FAILED, exit_code=0)
    failed_with_code = make_member(status=EvidenceStatus.FAILED, exit_code=1)
    assert failed_with_code.exit_code == 1
    failed_timed_out = make_member(status=EvidenceStatus.FAILED, exit_code=None)
    assert failed_timed_out.exit_code is None

    # SKIPPED and CANCELLED allow None or strict int
    skipped_none = make_member(status=EvidenceStatus.SKIPPED, exit_code=None)
    assert skipped_none.exit_code is None
    cancelled_int = make_member(status=EvidenceStatus.CANCELLED, exit_code=130)
    assert cancelled_int.exit_code == 130


def test_validation_member_rejects_naive_or_reversed_datetimes() -> None:
    naive_time = datetime(2026, 9, 6, 12, 0, 0)  # noqa: DTZ001
    aware_time = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(started_at=naive_time, completed_at=aware_time)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(started_at=aware_time, completed_at=naive_time)
    # completed < started
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(
            started_at=aware_time,
            completed_at=aware_time - timedelta(seconds=1),
        )


def test_validation_member_dst_fold_backwards_utc_rejects() -> None:
    tz = ZoneInfo("Europe/London")
    started = datetime(2026, 10, 25, 1, 15, tzinfo=tz, fold=1)
    completed = datetime(2026, 10, 25, 1, 45, tzinfo=tz, fold=0)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(started_at=started, completed_at=completed)


def test_validation_member_dst_fold_forwards_utc_accepts_and_roundtrips() -> None:
    tz = ZoneInfo("Europe/London")
    started = datetime(2026, 10, 25, 1, 45, tzinfo=tz, fold=0)
    completed = datetime(2026, 10, 25, 1, 15, tzinfo=tz, fold=1)
    member = make_member(started_at=started, completed_at=completed)
    assert member.started_at == started
    assert member.completed_at == completed

    manifest = make_validation_manifest(members=(member,))
    encoded = encode_evidence_manifest(manifest)
    decoded = decode_evidence_manifest(encoded)
    assert isinstance(decoded, ValidationEvidenceManifest)
    assert len(decoded.members) == 1
    decoded_member = decoded.members[0]
    assert decoded_member.result_id == member.result_id
    assert decoded_member.started_at == started.astimezone(UTC)
    assert decoded_member.completed_at == completed.astimezone(UTC)
    assert encode_evidence_manifest(decoded) == encoded


def test_validation_member_bounded_and_strict_fields() -> None:
    # nil UUID
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(result_id=_NIL_UUID)

    # blank or > 255 check_name / command_name
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(check_name="")
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(check_name="   ")
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(check_name="x" * 256)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(command_name="")

    # command_version positive strict int
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(command_version=0)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(command_version=-1)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(command_version=True)  # type: ignore[arg-type]

    # digests must be lowercase 64 hex
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(command_digest="A" * 64)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(command_digest="123")


# ---------------------------------------------------------------------------
# ValidationEvidenceManifest unit tests
# ---------------------------------------------------------------------------


def test_validation_manifest_schema_and_policy_bounds() -> None:
    # schema_version must be exactly 1, not bool
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        ValidationEvidenceManifest(
            schema_version=2,  # type: ignore[arg-type]
            kind="validation",
            evidence_set_id=_FIXED_UUID_1,
            run_id=_FIXED_UUID_2,
            step_id=_FIXED_UUID_3,
            policy_version=1,
            head_sha="a" * 40,
        )
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        ValidationEvidenceManifest(
            schema_version=True,  # type: ignore[arg-type]
            kind="validation",
            evidence_set_id=_FIXED_UUID_1,
            run_id=_FIXED_UUID_2,
            step_id=_FIXED_UUID_3,
            policy_version=1,
            head_sha="a" * 40,
        )

    # policy_version positive strict int <= 2147483647
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(policy_version=0)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(policy_version=-1)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(policy_version=2_147_483_648)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(policy_version=True)  # type: ignore[arg-type]

    # head_sha lowercase 40 hex
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(head_sha="A" * 40)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(head_sha="a" * 39)


def test_validation_manifest_self_parent_and_nil_rejected() -> None:
    # Nil UUIDs
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(evidence_set_id=_NIL_UUID)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(prior_review_evidence_set_id=_NIL_UUID)

    # Self-parent: prior_review_evidence_set_id == evidence_set_id
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(
            evidence_set_id=_FIXED_UUID_1,
            prior_review_evidence_set_id=_FIXED_UUID_1,
        )


def test_validation_manifest_members_bounds_and_deduplication() -> None:
    # max 64 members
    m_base = make_member()
    members_65 = tuple(
        m_base.model_copy(
            update={
                "result_id": uuid4(),
                "check_name": f"check_{i}",
                "command_name": f"cmd_{i}",
            }
        )
        for i in range(65)
    )
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(members=members_65)

    # zero members is explicitly allowed
    empty_manifest = make_validation_manifest(members=())
    assert len(empty_manifest.members) == 0

    # duplicate result_id rejected
    shared_id = uuid4()
    m1 = make_member(result_id=shared_id, check_name="c1", command_name="cmd1")
    m2 = make_member(result_id=shared_id, check_name="c2", command_name="cmd2")
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(members=(m1, m2))

    # duplicate semantic identity (check_name, command_name, command_version) rejected
    m3 = make_member(result_id=uuid4(), check_name="c1", command_name="cmd1", command_version=1)
    m4 = make_member(result_id=uuid4(), check_name="c1", command_name="cmd1", command_version=1)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_validation_manifest(members=(m3, m4))


def test_validation_manifest_canonical_ordering_and_caller_immutability() -> None:
    m1 = make_member(
        result_id=_FIXED_UUID_1, check_name="zeta", command_name="cmd", command_version=1
    )
    m2 = make_member(
        result_id=_FIXED_UUID_2, check_name="alpha", command_name="cmd", command_version=1
    )
    m3 = make_member(
        result_id=_FIXED_UUID_3, check_name="alpha", command_name="cmd", command_version=2
    )

    original_list = [m1, m2, m3]
    manifest = make_validation_manifest(members=tuple(original_list))

    # Expect canonical sort: (check_name, command_name, command_version, result_id)
    # alpha:v1, alpha:v2, zeta:v1
    assert manifest.members[0].check_name == "alpha" and manifest.members[0].command_version == 1
    assert manifest.members[1].check_name == "alpha" and manifest.members[1].command_version == 2
    assert manifest.members[2].check_name == "zeta" and manifest.members[2].command_version == 1

    # Caller list untouched
    assert original_list[0] is m1
    assert original_list[1] is m2
    assert original_list[2] is m3


# ---------------------------------------------------------------------------
# ReviewEvidenceManifest unit tests
# ---------------------------------------------------------------------------


def test_review_manifest_self_parent_and_nil_rejected() -> None:
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_review_manifest(evidence_set_id=_NIL_UUID)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_review_manifest(validation_evidence_set_id=_NIL_UUID)
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_review_manifest(producer_execution_id=_NIL_UUID)

    # Self-parent: validation_evidence_set_id == evidence_set_id
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_review_manifest(
            evidence_set_id=_FIXED_UUID_1,
            validation_evidence_set_id=_FIXED_UUID_1,
        )


def test_review_manifest_naive_resolved_at_rejected() -> None:
    naive_resolved = datetime(2026, 9, 6, 12, 0, 0)  # noqa: DTZ001
    finding = ReviewFinding(
        finding_id="F1",
        severity=FindingSeverity.MINOR,
        path="foo.py",
        start_line=10,
        summary="nit",
        evidence="sample",
        resolved_at=naive_resolved,
    )
    review = ReviewOutput(
        decision=ReviewDecision.APPROVE,
        findings=(finding,),
        tested_claims=("claims",),
        missing_evidence=(),
        summary="approved with resolved finding",
    )
    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_review_manifest(review=review)


def test_review_manifest_canonicalizes_findings_and_preserves_caller() -> None:
    f2 = ReviewFinding(
        finding_id="finding-2",
        severity=FindingSeverity.MINOR,
        path="foo.py",
        start_line=20,
        summary="nit 2",
        evidence="sample 2",
        resolved_at=_SAMPLE_RESOLVED,
    )
    f1 = ReviewFinding(
        finding_id="finding-1",
        severity=FindingSeverity.MINOR,
        path="foo.py",
        start_line=10,
        summary="nit 1",
        evidence="sample 1",
        resolved_at=_SAMPLE_RESOLVED,
    )
    review = ReviewOutput(
        decision=ReviewDecision.APPROVE,
        findings=(f2, f1),
        tested_claims=("claims",),
        missing_evidence=(),
        summary="approved",
    )
    manifest = make_review_manifest(review=review)

    # Sorted by finding_id
    assert [f.finding_id for f in manifest.review.findings] == ["finding-1", "finding-2"]
    # Caller object not mutated
    assert [f.finding_id for f in review.findings] == ["finding-2", "finding-1"]


# ---------------------------------------------------------------------------
# Codec and digest tests
# ---------------------------------------------------------------------------


def test_encode_decode_roundtrip_validation_manifest() -> None:
    m = make_member(result_id=_FIXED_UUID_4)
    manifest = make_validation_manifest(members=(m,))

    encoded = encode_evidence_manifest(manifest)
    assert isinstance(encoded, bytes)

    decoded = decode_evidence_manifest(encoded)
    assert isinstance(decoded, ValidationEvidenceManifest)
    assert decoded == manifest


def test_encode_decode_roundtrip_zero_finding_review_manifest() -> None:
    manifest = make_review_manifest()
    encoded = encode_evidence_manifest(manifest)
    assert isinstance(encoded, bytes)

    decoded = decode_evidence_manifest(encoded)
    assert isinstance(decoded, ReviewEvidenceManifest)
    assert decoded == manifest
    assert decoded.review.decision == ReviewDecision.APPROVE
    assert len(decoded.review.findings) == 0


def test_golden_fixture_validation_manifest() -> None:
    # Uses Unicode check_name and non-UTC timezone (+02:00) to verify canonicalization
    plus_two_tz = timezone(timedelta(hours=2))
    member = ValidationEvidenceMember(
        result_id=UUID("00000000-0000-0000-0000-000000000001"),
        check_name="pytest-测试",
        command_name="run_tests",
        command_version=1,
        command_digest="0" * 64,
        command_result_digest="1" * 64,
        stdout_digest="2" * 64,
        stderr_digest="3" * 64,
        status=EvidenceStatus.PASSED,
        exit_code=0,
        started_at=datetime(2026, 9, 6, 14, 0, 0, tzinfo=plus_two_tz),
        completed_at=datetime(2026, 9, 6, 14, 1, 0, tzinfo=plus_two_tz),
    )
    manifest = ValidationEvidenceManifest(
        schema_version=1,
        kind="validation",
        evidence_set_id=UUID("00000000-0000-0000-0000-00000000000a"),
        run_id=UUID("00000000-0000-0000-0000-00000000000b"),
        step_id=UUID("00000000-0000-0000-0000-00000000000c"),
        policy_version=1,
        head_sha="f" * 40,
        prior_review_evidence_set_id=None,
        members=(member,),
    )

    encoded = encode_evidence_manifest(manifest)
    digest = evidence_manifest_digest(manifest)

    # Literal expected canonical bytes and hardcoded SHA-256 digest
    expected_bytes = (
        b'{"evidence_set_id":"00000000-0000-0000-0000-00000000000a",'
        b'"head_sha":"ffffffffffffffffffffffffffffffffffffffff",'
        b'"kind":"validation",'
        b'"members":[{"check_name":"pytest-\xe6\xb5\x8b\xe8\xaf\x95",'
        b'"command_digest":"0000000000000000000000000000000000000000000000000000000000000000",'
        b'"command_name":"run_tests",'
        b'"command_result_digest":"1111111111111111111111111111111111111111111111111111111111111111",'
        b'"command_version":1,'
        b'"completed_at":"2026-09-06T12:01:00+00:00",'
        b'"exit_code":0,'
        b'"result_id":"00000000-0000-0000-0000-000000000001",'
        b'"started_at":"2026-09-06T12:00:00+00:00",'
        b'"status":"PASSED",'
        b'"stderr_digest":"3333333333333333333333333333333333333333333333333333333333333333",'
        b'"stdout_digest":"2222222222222222222222222222222222222222222222222222222222222222"}],'
        b'"policy_version":1,'
        b'"prior_review_evidence_set_id":null,'
        b'"run_id":"00000000-0000-0000-0000-00000000000b",'
        b'"schema_version":1,'
        b'"step_id":"00000000-0000-0000-0000-00000000000c"}'
    )
    expected_digest = "09217581352fd105b52fe1606cb492bee1d375a4d28e221729a1360fe5f04ddd"

    assert encoded == expected_bytes
    assert digest == expected_digest

    # Verify roundtrip through decoder
    decoded = decode_evidence_manifest(encoded)
    assert decoded == manifest
    assert evidence_manifest_digest(decoded) == expected_digest


def test_ordering_invariance_produces_identical_digest() -> None:
    m1 = make_member(result_id=_FIXED_UUID_1, check_name="b", command_name="c", command_version=1)
    m2 = make_member(result_id=_FIXED_UUID_2, check_name="a", command_name="c", command_version=1)

    v1 = make_validation_manifest(members=(m1, m2))
    v2 = make_validation_manifest(members=(m2, m1))

    assert encode_evidence_manifest(v1) == encode_evidence_manifest(v2)
    assert evidence_manifest_digest(v1) == evidence_manifest_digest(v2)


def test_changed_binding_output_status_changes_digest() -> None:
    base = make_validation_manifest(members=(make_member(result_id=_FIXED_UUID_4),))
    base_digest = evidence_manifest_digest(base)

    # Changed run_id
    assert evidence_manifest_digest(base.model_copy(update={"run_id": uuid4()})) != base_digest
    # Changed step_id
    assert evidence_manifest_digest(base.model_copy(update={"step_id": uuid4()})) != base_digest
    # Changed policy_version
    assert evidence_manifest_digest(base.model_copy(update={"policy_version": 2})) != base_digest
    # Changed head_sha
    assert evidence_manifest_digest(base.model_copy(update={"head_sha": "0" * 40})) != base_digest
    # Changed member status
    m_failed = make_member(result_id=_FIXED_UUID_4, status=EvidenceStatus.FAILED, exit_code=1)
    assert evidence_manifest_digest(base.model_copy(update={"members": (m_failed,)})) != base_digest


def test_decode_rejects_non_canonical_wire_representations() -> None:
    manifest = make_validation_manifest(members=(make_member(result_id=_FIXED_UUID_4),))
    canonical_bytes = encode_evidence_manifest(manifest)

    # 1. Wire representation with extra whitespace
    wire_with_space = canonical_bytes.replace(b'{"evidence_set_id":', b'{ "evidence_set_id":')
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(wire_with_space)

    # 2. Wire representation with trailing newline
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(canonical_bytes + b"\n")

    # 3. Wire representation with unsorted keys
    data_dict = json.loads(canonical_bytes)
    # Reverse top-level key order
    reversed_keys_json = json.dumps(
        dict(reversed(list(data_dict.items()))),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    if reversed_keys_json != canonical_bytes:
        with pytest.raises(EvidenceManifestError):
            decode_evidence_manifest(reversed_keys_json)

    # 4. Wire representation with Z instead of +00:00
    if b"+00:00" in canonical_bytes:
        wire_with_z = canonical_bytes.replace(b"+00:00", b"Z")
        with pytest.raises(EvidenceManifestError):
            decode_evidence_manifest(wire_with_z)


def test_decode_rejects_duplicate_keys_recursively() -> None:
    # Duplicate key at top level
    dup_top = b'{"kind":"validation","kind":"validation","schema_version":1}'
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(dup_top)

    # Duplicate key nested in member
    dup_nested = (
        b'{"evidence_set_id":"11111111-1111-1111-1111-111111111111",'
        b'"head_sha":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        b'"kind":"validation",'
        b'"members":[{"check_name":"a","check_name":"b"}],'
        b'"policy_version":1,'
        b'"prior_review_evidence_set_id":null,'
        b'"run_id":"22222222-2222-2222-2222-222222222222",'
        b'"schema_version":1,'
        b'"step_id":"33333333-3333-3333-3333-333333333333"}'
    )
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(dup_nested)


def test_decode_rejects_oversize_before_parse() -> None:
    # 256 KiB = 262_144 bytes; oversize = 262_145 bytes
    huge_data = b" " * (256 * 1024 + 1)
    with pytest.raises(EvidenceManifestError) as exc_info:
        decode_evidence_manifest(huge_data)
    assert "256 KiB" in str(exc_info.value) or "exceeds maximum size" in str(exc_info.value)


def test_decode_rejects_non_object_root() -> None:
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(b"[]")
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(b'"string"')
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(b"123")
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(b"true")
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(b"null")


def test_decode_rejects_nan_infinity() -> None:
    payload = b'{"schema_version":NaN,"kind":"validation"}'
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(payload)

    payload_inf = b'{"schema_version":Infinity,"kind":"validation"}'
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(payload_inf)


def test_decode_rejects_boolean_integer_coercion() -> None:
    # schema_version as bool
    manifest = make_validation_manifest()
    encoded = encode_evidence_manifest(manifest)

    bool_schema = encoded.replace(b'"schema_version":1', b'"schema_version":true')
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(bool_schema)

    # policy_version as bool
    bool_policy = encoded.replace(b'"policy_version":1', b'"policy_version":true')
    with pytest.raises(EvidenceManifestError):
        decode_evidence_manifest(bool_policy)


def test_discriminated_union_membership() -> None:
    v_manifest = make_validation_manifest()
    r_manifest = make_review_manifest()

    assert isinstance(v_manifest, ValidationEvidenceManifest)
    assert isinstance(r_manifest, ReviewEvidenceManifest)

    # Both conform to EvidenceManifest type union
    items: list[EvidenceManifest] = [v_manifest, r_manifest]
    assert len(items) == 2


def test_error_message_does_not_leak_raw_payload_or_secrets() -> None:
    secret = "ghp_supersecretgithubtoken1234567890"
    malformed_json_with_secret = f'{{"evidence_set_id":"bad_uuid","secret":"{secret}"}}'.encode()

    with pytest.raises(EvidenceManifestError) as exc_info:
        decode_evidence_manifest(malformed_json_with_secret)

    msg = str(exc_info.value)
    diag = repr(exc_info.value)
    assert secret not in msg
    assert secret not in diag


def test_encode_rejects_model_copy_bool_schema() -> None:
    manifest = make_validation_manifest()
    copied = manifest.model_copy(update={"schema_version": True})
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(copied)
    with pytest.raises(EvidenceManifestError):
        evidence_manifest_digest(copied)


def test_encode_rejects_model_copy_nested_member_command_version_zero() -> None:
    member = make_member()
    copied_member = member.model_copy(update={"command_version": 0})
    manifest = make_validation_manifest()
    invalid_manifest = manifest.model_copy(update={"members": (copied_member,)})
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(invalid_manifest)
    with pytest.raises(EvidenceManifestError):
        evidence_manifest_digest(invalid_manifest)


def test_encode_rejects_incomplete_constructed_root_and_member_safely() -> None:
    incomplete_root = ValidationEvidenceManifest.model_construct(kind="validation")
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(incomplete_root)

    incomplete_member = ValidationEvidenceMember.model_construct(check_name="pytest")
    manifest = make_validation_manifest().model_copy(update={"members": (incomplete_member,)})
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(manifest)


def test_encode_rejects_unknown_nested_model_state_without_dropping_it() -> None:
    member = make_member().model_copy(update={"unexpected_member": "value"})
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(
            make_validation_manifest().model_copy(update={"members": (member,)})
        )

    finding = ReviewFinding(
        finding_id="finding-1",
        severity=FindingSeverity.MINOR,
        path="foo.py",
        start_line=1,
        summary="summary",
        evidence="evidence",
    ).model_copy(update={"unexpected_finding": "value"})
    review = ReviewOutput(
        decision=ReviewDecision.APPROVE,
        findings=(finding,),
        tested_claims=("claims",),
        missing_evidence=(),
        summary="approved",
    ).model_copy(update={"unexpected_review": "value"})
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(make_review_manifest().model_copy(update={"review": review}))

    finding_only_review = ReviewOutput(
        decision=ReviewDecision.APPROVE,
        findings=(finding,),
        tested_claims=("claims",),
        missing_evidence=(),
        summary="approved",
    )
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(
            make_review_manifest().model_copy(update={"review": finding_only_review})
        )


def test_review_dict_rejects_unknown_or_missing_required_fields() -> None:
    review = {
        "decision": "approve",
        "findings": [],
        "tested_claims": ["claims"],
        "missing_evidence": [],
        "summary": "approved",
    }
    with pytest.raises((ValueError, TypeError, ValidationError)):
        make_review_manifest(review={**review, "unexpected": "value"})
    with pytest.raises((ValueError, TypeError, ValidationError)):
        make_review_manifest(
            review={key: value for key, value in review.items() if key != "summary"}
        )


def test_encode_rejects_model_copy_invalid_review_decision_invariants() -> None:
    review = ReviewOutput(
        decision=ReviewDecision.APPROVE,
        findings=(),
        tested_claims=("claims pass",),
        missing_evidence=(),
        summary="valid approve",
    )
    invalid_review = review.model_copy(update={"missing_evidence": ("missing-something",)})
    manifest = make_review_manifest()
    invalid_manifest = manifest.model_copy(update={"review": invalid_review})
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(invalid_manifest)
    with pytest.raises(EvidenceManifestError):
        evidence_manifest_digest(invalid_manifest)


def test_manifest_construction_prevents_caller_list_mutation() -> None:
    claims = ["first claim"]
    review = ReviewOutput.model_construct(
        decision=ReviewDecision.APPROVE,
        findings=(),
        tested_claims=claims,
        missing_evidence=(),
        summary="approved",
    )
    manifest = make_review_manifest(review=review)
    claims.append("appended claim")
    assert manifest.review.tested_claims == ("first claim",)


def test_decode_rejects_oversized_integer_safely() -> None:
    huge_int_json = (
        b'{"schema_version":1,"kind":"validation","policy_version":' + b"9" * 5000 + b"}"
    )
    with pytest.raises(EvidenceManifestError) as exc_info:
        decode_evidence_manifest(huge_int_json)
    assert isinstance(exc_info.value, EvidenceManifestError)


def test_decode_rejects_deeply_nested_array_safely() -> None:
    deep_json = b"[" * 100000 + b"]" * 100000
    with pytest.raises(EvidenceManifestError) as exc_info:
        decode_evidence_manifest(deep_json)
    assert isinstance(exc_info.value, EvidenceManifestError)


def test_utc_overflow_datetime_handled_safely() -> None:
    plus_one_tz = timezone(timedelta(hours=1))
    year1_time = datetime(1, 1, 1, 0, 0, 0, tzinfo=plus_one_tz)

    with pytest.raises((ValueError, TypeError, ValidationError, EvidenceManifestError)):
        make_member(started_at=year1_time)

    member = make_member().model_copy(update={"started_at": year1_time})
    manifest = make_validation_manifest()
    invalid_manifest = manifest.model_copy(update={"members": (member,)})
    with pytest.raises(EvidenceManifestError):
        encode_evidence_manifest(invalid_manifest)


def test_encode_surrogate_string_handled_safely() -> None:
    surrogate_str = "invalid_\ud800_surrogate"
    member = make_member().model_copy(update={"check_name": surrogate_str})
    manifest = make_validation_manifest()
    invalid_manifest = manifest.model_copy(update={"members": (member,)})
    with pytest.raises(EvidenceManifestError) as exc_info:
        encode_evidence_manifest(invalid_manifest)
    assert surrogate_str not in str(exc_info.value)
    assert surrogate_str not in repr(exc_info.value)
