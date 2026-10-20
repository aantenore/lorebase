"""Typed failures of the commit pipeline. Any failure leaves nothing visible."""

from __future__ import annotations

from collections.abc import Sequence

from lorebase_core.validate import Issue


class CommitError(Exception):
    """Base class of commit failures."""


class BranchNotFoundError(CommitError):
    """The branch does not exist (or the ref is a tag, which never moves)."""


class BranchNotMaterializedError(CommitError):
    """The branch has no materialized head to validate against."""


class StaleHeadError(CommitError):
    """The branch head is not the one the caller expected (or moved during the commit)."""

    def __init__(self, branch: str, expected: bytes, actual: bytes | None) -> None:
        actual_text = "unknown" if actual is None else actual.hex()
        super().__init__(f"branch {branch!r} is at {actual_text}, expected {expected.hex()}")
        self.branch = branch
        self.expected = expected
        self.actual = actual


class BranchLockTimeoutError(CommitError):
    """Another writer held the branch lock longer than the configured timeout."""


class SchemaChangeError(CommitError):
    """A schema change cannot be applied (unknown element, Domain removal, wrong Domain)."""


class ValidationFailedError(CommitError):
    """The changes violate the schema or the existing data; nothing was written."""

    def __init__(self, issues: Sequence[Issue]) -> None:
        super().__init__(f"{len(issues)} validation issue(s); first: {issues[0].message}")
        self.issues = list(issues)
