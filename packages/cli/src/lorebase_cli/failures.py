"""Exit codes and the text of failures.

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | usage or configuration error, a thing that does not exist (404), any other refused request |
| 2 | validation failure (422) or conflict (409): the request is well formed but cannot apply |
| 3 | authentication or permission failure (401, 403) |
| 4 | server error (5xx) or server unreachable |

Failures go to standard error; with `--json` they are a JSON document there, so that standard
output only ever holds results.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from lorebase_client import (
    AuthenticationError,
    ConflictError,
    LorebaseConnectionError,
    LorebaseError,
    PermissionDeniedError,
    ServerError,
    ValidationFailedError,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INVALID = 2
EXIT_AUTH = 3
EXIT_SERVER = 4


class CliError(Exception):
    """A failure with a message for the user and an exit code (default: usage error)."""

    def __init__(self, message: str, *, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


Locator = Callable[[Sequence[Any]], str | None]


class ImportAbortedError(CliError):
    """An import stopped at a rejected batch; earlier batches stay committed.

    `locate` turns the path of an issue into a place in the input files, for imports that read
    several (the default numbers the rows of the one file); `scope` words which rows were rejected.
    """

    def __init__(
        self,
        cause: LorebaseError,
        *,
        first_row: int,
        last_row: int,
        committed_rows: int,
        committed_revisions: list[str],
        scope: str | None = None,
        locate: Locator | None = None,
    ) -> None:
        super().__init__(
            f"{scope or f'rows {first_row}-{last_row} were rejected'}; {committed_rows} earlier "
            "rows are committed",
            exit_code=exit_code_for(cause),
        )
        self.cause = cause
        self.locate = locate
        self.first_row = first_row
        self.last_row = last_row
        self.committed_rows = committed_rows
        self.committed_revisions = committed_revisions


def exit_code_for(error: LorebaseError) -> int:
    """The exit code of a client error."""
    if isinstance(error, ValidationFailedError | ConflictError):
        return EXIT_INVALID
    if isinstance(error, AuthenticationError | PermissionDeniedError):
        return EXIT_AUTH
    if isinstance(error, ServerError | LorebaseConnectionError):
        return EXIT_SERVER
    return EXIT_USAGE


def _location(path: list[Any], row_offset: int) -> str:
    """`rows[12].supplier_code`-style text for an issue path; row indexes are shifted by
    `row_offset` (the number of the first row sent, for imports)."""
    text = ""
    previous: Any = None
    for part in path:
        if isinstance(part, int):
            number = part + row_offset if previous == "rows" else part
            text += f"[{number}]"
        else:
            text += f".{part}" if text else str(part)
        previous = part
    return text


def issue_lines(
    error: LorebaseError,
    *,
    row_offset: int = 0,
    limit: int = 20,
    locate: Locator | None = None,
) -> list[str]:
    """One line per validation finding or invalid request member."""
    lines: list[str] = []
    for issue in error.issues[:limit]:
        path = list(issue.get("path") or [])
        where = (locate(path) if locate else None) or _location(path, row_offset)
        subject = " ".join(
            str(issue[name]) for name in ("kind", "relation", "key", "attribute") if issue.get(name)
        )
        prefix = f"{where}: " if where else ""
        suffix = f" [{issue['code']}]" if issue.get("code") else ""
        lines.append(
            f"  {prefix}{issue.get('message', '')}{suffix}" + (f" ({subject})" if subject else "")
        )
    for member in error.errors[:limit]:
        lines.append(f"  {member.get('pointer', '')}: {member.get('message', '')}")
    total = len(error.issues) + len(error.errors)
    if total > limit:
        lines.append(f"  ... and {total - limit} more")
    return lines


def describe(
    error: LorebaseError, *, row_offset: int = 0, locate: Locator | None = None
) -> list[str]:
    """Lines to print for an error: the problem detail, what it carries and a hint."""
    lines = [f"error: {error.message}"]
    lines.extend(issue_lines(error, row_offset=row_offset, locate=locate))
    if error.current_head:
        lines.append(f"  the branch is now at {error.current_head}; read it again and retry")
    if error.extra.get("proposals"):
        lines.append(f"  open a Proposal at {error.extra['proposals']} instead")
    if error.extra.get("permission"):
        lines.append(f"  missing permission: {error.extra['permission']}")
    if error.retry_after:
        lines.append(f"  retry after {error.retry_after:g} s")
    if error.request_id:
        lines.append(f"  request id: {error.request_id}")
    return lines


def render_failure(error: Exception, *, json_mode: bool) -> tuple[str, int]:
    """The text for standard error and the exit code of a failure."""
    row_offset = 0
    locate: Locator | None = None
    committed: dict[str, Any] = {}
    api_error: LorebaseError | None = None
    notes: list[str] = []
    if isinstance(error, ImportAbortedError):
        api_error = error.cause
        row_offset = error.first_row  # 1-based row numbers of the input file
        locate = error.locate
        committed = {
            "committed_rows": error.committed_rows,
            "committed_revisions": error.committed_revisions,
            "first_row": error.first_row,
            "last_row": error.last_row,
        }
        notes = [error.message]
        code = error.exit_code
    elif isinstance(error, LorebaseError):
        api_error = error
        code = exit_code_for(error)
    elif isinstance(error, CliError):
        code = error.exit_code
    else:
        raise error
    if json_mode:
        body: dict[str, Any] = {"message": str(error), **committed}
        if api_error is not None:
            body.update(
                status=api_error.status,
                code=api_error.code,
                type=api_error.type,
                detail=api_error.detail,
                request_id=api_error.request_id,
                extra=api_error.extra,
            )
        return json.dumps({"error": body}, indent=2), code
    if api_error is None:
        return f"error: {error}", code
    lines = describe(api_error, row_offset=row_offset, locate=locate)
    return "\n".join([*lines, *(f"  {note}" for note in notes)]), code
