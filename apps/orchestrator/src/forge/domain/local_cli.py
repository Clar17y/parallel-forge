"""How the operator admits locally configured CLI clients."""

from enum import StrEnum


class LocalCliTrust(StrEnum):
    OPERATOR = "operator"
    VERIFIED = "verified"


OPERATOR_TRUST_WARNING = (
    "This local client is trusted by the operator. Forge uses the configured login, "
    "model and spending controls without independently verifying provider internals."
)
