from __future__ import annotations

import pytest
from forge.release.credentials import GitHubCredentialError, LocalGitHubCredentialResolver


class _Store:
    def create(self, secret_id: str, secret: bytes) -> None:
        pass

    def read(self, secret_id: str) -> bytes:
        return b"github_pat_this_is_a_test_token_with_enough_length"

    def exists(self, secret_id: str) -> bool:
        return True

    def delete(self, secret_id: str) -> None:
        pass


@pytest.mark.asyncio
async def test_github_credential_failures_and_representations_do_not_reveal_reference_or_value() -> (
    None
):
    resolver = LocalGitHubCredentialResolver(_Store())
    with pytest.raises(GitHubCredentialError) as error:
        await resolver.resolve("secret://forge/Bad.Ref")
    assert "Bad.Ref" not in str(error.value)
    assert "github_pat" not in repr(resolver)
