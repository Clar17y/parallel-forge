"""Optional profile defaults retain the exact legacy record representation."""

from dataclasses import replace
from uuid import uuid4

from forge.domain.policy import JevPolicy
from forge.domain.subscription import (
    OperatorProfile,
    decode_subscription_record,
    encode_subscription_record,
    subscription_record_fingerprint,
)


def test_profile_jev_roundtrip_and_legacy_encoding():
    original = OperatorProfile(profile_id=uuid4(), version=1, preferences=())
    legacy = encode_subscription_record(original)
    assert "jev" not in dict(legacy["record"]["fields"])
    configured = replace(
        original, jev=JevPolicy(mode="on", allow_remote=True, timeout_seconds=2.5)
    )
    assert decode_subscription_record(encode_subscription_record(configured)) == configured
    assert encode_subscription_record(decode_subscription_record(legacy)) == legacy
    assert subscription_record_fingerprint(configured) != subscription_record_fingerprint(original)
    assert encode_subscription_record(replace(configured, jev=None)) == legacy
