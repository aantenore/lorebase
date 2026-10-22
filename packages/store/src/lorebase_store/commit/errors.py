"""Typed failures of the commit pipeline. Any failure leaves nothing visible."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from lorebase_core.validate import Issue


class CommitError(Exception):
    """Base class of commit failures."""


class BranchNotFoundError(CommitError):
    """The branch does not exist (or the ref is a tag, which never moves)."""


class BranchNotMaterializedError(CommitError):
    """The branch has no materialized head to validate against."""


class BranchMaterializingError(CommitError):
    """The branch is still being copied in the background, and the change (a schema change)
    needs its full copy: retry once its materialization is done."""


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


class SensitivityLoweredError(CommitError):
    """The change removes sensitivity tags (or sensitive Attributes) without having been
    allowed to: only callers cleared for the tags may (design 10.2)."""

    def __init__(self, tags: Iterable[str]) -> None:
        self.tags = sorted(tags)
        super().__init__(f"the change removes the sensitivity tags {', '.join(self.tags)}")


class ValidationFailedError(CommitError):
    """The changes violate the schema or the existing data; nothing was written."""

    def __init__(self, issues: Sequence[Issue]) -> None:
        super().__init__(f"{len(issues)} validation issue(s); first: {issues[0].message}")
        self.issues = list(issues)
