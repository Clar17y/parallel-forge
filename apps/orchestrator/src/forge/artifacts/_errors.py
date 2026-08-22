"""Shared artifact adapter errors without platform import cycles."""


class ArtifactIntegrityError(RuntimeError):
    """A blob is missing, linked, nonregular, or does not match its digest."""


class ArtifactStoreError(RuntimeError):
    """A safe artifact publication could not be completed."""


__all__ = ["ArtifactIntegrityError", "ArtifactStoreError"]
