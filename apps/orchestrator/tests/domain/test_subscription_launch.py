"""Terminal launch proofs reject coercion, ambiguous stop state and open metadata."""

import pytest
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof


def payload():
    return {
        "launch_id": "launch",
        "pid": 123,
        "process_identity": "start",
        "outcome": "exited",
        "return_code": 0,
        "stop_confirmed": True,
        "stdout_bytes": 10,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"pid": True},
        {"stop_confirmed": 1},
        {"stdout_bytes": -1},
        {"outcome": "unknown"},
        {"return_code": None},
        {"launch_id": "x" * 256},
        {"process_identity": ""},
        {"native_output": "must not persist"},
        {"outcome": "stop_uncertain"},
    ],
)
def test_launch_proof_rejects_malformed_terminal(change):
    with pytest.raises(ValueError):
        SubscriptionLaunchTerminalProof.model_validate(payload() | change)


def test_launch_proof_is_immutable_and_round_trips():
    proof = SubscriptionLaunchTerminalProof.model_validate(payload())
    assert proof.permits_decision
    assert SubscriptionLaunchTerminalProof.model_validate(proof.model_dump(mode="json")) == proof
    with pytest.raises(ValueError):
        proof.pid = 999
