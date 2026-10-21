"""Errors raised by the inference providers."""

from __future__ import annotations

from collections.abc import Sequence


class InferenceError(Exception):
    """Base class of every error raised by this package."""


class EndpointError(InferenceError):
    """An HTTP model endpoint failed or answered with something unusable.

    The message holds the status and the error text sent by the server (truncated); it never
    holds prompts or credentials.
    """

    def __init__(self, message: str, *, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail
        """Error text sent by the server, truncated; used to recognize unsupported options."""


class InvalidOutputError(InferenceError):
    """The model answered, but not with JSON that satisfies the requested schema."""

    def __init__(self, message: str, *, problems: Sequence[str] = (), attempts: int = 1) -> None:
        super().__init__(message)
        self.problems = tuple(problems)
        """Validation problems of the last answer: schema path and violated keyword."""
        self.attempts = attempts


class DimensionMismatchError(InferenceError):
    """A model returned vectors whose size differs from the configured dimension."""


class ProviderUnavailableError(InferenceError):
    """A configured provider needs an optional dependency that is not installed."""
