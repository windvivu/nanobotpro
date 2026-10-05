"""Stable errors for storage and adapter boundaries."""


class BrainMemoryError(Exception):
    """Base error; adapters may fall back to legacy memory."""


class ScopeError(BrainMemoryError, ValueError):
    """A reference is not confined to its configured root."""


class DisabledError(BrainMemoryError):
    """A write was attempted while Brain Memory is disabled."""


class ConflictError(BrainMemoryError):
    """An immutable record already exists with different content."""


class FormatError(BrainMemoryError, ValueError):
    """Unsupported or malformed data; never silently discard source content."""
