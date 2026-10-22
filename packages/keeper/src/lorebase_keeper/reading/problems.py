"""Problem details of the read path."""

from __future__ import annotations

from pydantic import ValidationError

from lorebase_core.query import QueryError
from lorebase_keeper.errors import VALIDATION_FAILED, ProblemError


def query_problem(error: QueryError, prefix: str) -> ProblemError:
    """422 listing every issue of a query; pointers are prefixed with where the query sits."""
    return ProblemError(
        VALIDATION_FAILED,
        "The query does not fit the schema or the configured limits.",
        extensions={
            "errors": [
                {
                    "pointer": prefix + ("" if issue.pointer == "/" else issue.pointer),
                    "message": issue.message,
                    "code": issue.code,
                }
                for issue in error.issues
            ]
        },
    )


def invalid(pointer: str, message: str, code: str = "invalid") -> ProblemError:
    """422 for one request member."""
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={"errors": [{"pointer": pointer, "message": message, "code": code}]},
    )


def tree_unsupported(what: str) -> ProblemError:
    """422 for a feature that reads at an arbitrary Revision do not offer (design 6.12)."""
    return ProblemError(
        VALIDATION_FAILED,
        f"{what} is not available for reads at a Revision other than the branch head; reads "
        "at a Revision support key lookups and Kind or Link listings ordered by key.",
        extensions={"errors": [{"pointer": "/query/at", "message": what, "code": "time_travel"}]},
    )


def snapshot_refused(what: str, entries: int, limit: int) -> ProblemError:
    """422 for a read at a Revision too large to snapshot (design 6.12)."""
    return ProblemError(
        VALIDATION_FAILED,
        f"{what} at a Revision other than the branch head reads a snapshot of that Revision; "
        f"this one holds {entries} Records and Links, more than the limit of {limit} "
        "(read.time_travel.snapshot_max_entries). Key lookups and listings ordered by key "
        "remain available.",
        extensions={"errors": [{"pointer": "/query/at", "message": what, "code": "time_travel"}]},
    )


def request_problem(error: ValidationError, prefix: str) -> ProblemError:
    """422 for a request part validated outside FastAPI (for example a JSON query parameter)."""
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={
            "errors": [
                {
                    "pointer": prefix + "".join(f"/{part}" for part in item["loc"]),
                    "message": item["msg"],
                    "code": item["type"],
                }
                for item in error.errors()
            ]
        },
    )
