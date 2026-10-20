"""Rendering of results: JSON for machines, tables for people.

Tables are plain aligned text by default (stable in pipes and tests); on a terminal they use
`rich` when it is installed (it comes with Typer). Long cells are cut at
`settings.max_cell_width`. Informational messages go to standard error so that standard output
only holds the result.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import IO, Any

ELLIPSIS = "…"


@dataclass(frozen=True)
class Column:
    """A table column: header, key of the value and optional formatter."""

    header: str
    key: str
    format: Callable[[Any], str] | None = None


def to_jsonable(value: Any) -> Any:
    """Plain JSON data of models, dates and decimals."""
    dump = getattr(value, "model_dump", None)  # a pydantic model of the client
    if callable(dump):
        return dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def cell(value: Any) -> str:
    """Text of a table cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, list | tuple):
        return ", ".join(cell(v) for v in value)
    if isinstance(value, Mapping):
        return json.dumps(to_jsonable(value), separators=(",", ":"), ensure_ascii=False)
    return str(value).replace("\n", " ")


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + ELLIPSIS


def short_id(value: str | None, length: int = 12) -> str:
    """The first characters of a Revision id."""
    return "" if value is None else value[:length]


class Output:
    """Where a command writes its result."""

    def __init__(
        self,
        *,
        json_mode: bool = False,
        max_cell_width: int = 60,
        stdout: IO[str] | None = None,
        stderr: IO[str] | None = None,
    ) -> None:
        self.json_mode = json_mode
        self.max_cell_width = max_cell_width
        self._stdout = stdout
        self._stderr = stderr

    @property
    def stdout(self) -> IO[str]:
        return self._stdout or sys.stdout

    @property
    def stderr(self) -> IO[str]:
        return self._stderr or sys.stderr

    def info(self, message: str) -> None:
        """A note for the person running the command (standard error)."""
        print(message, file=self.stderr)

    def text(self, text: str) -> None:
        """Raw text on standard output (a schema document)."""
        self.stdout.write(text if text.endswith("\n") else text + "\n")

    def data(self, payload: Any) -> None:
        """The JSON document of a result."""
        print(json.dumps(to_jsonable(payload), indent=2, ensure_ascii=False), file=self.stdout)

    def emit(self, payload: Any, human: Callable[[], None]) -> None:
        """JSON in JSON mode, otherwise whatever `human` prints."""
        if self.json_mode:
            self.data(payload)
        else:
            human()

    def table(
        self,
        rows: Sequence[Mapping[str, Any]],
        columns: Sequence[Column],
        *,
        truncate: bool = True,
    ) -> None:
        """Rows with the given columns (long cells cut unless `truncate` is off); nothing but
        the header for no rows."""
        grid = [
            [
                shorten(
                    column.format(row.get(column.key))
                    if column.format
                    else cell(row.get(column.key)),
                    self.max_cell_width if truncate else sys.maxsize,
                )
                for column in columns
            ]
            for row in rows
        ]
        headers = [column.header for column in columns]
        if self._use_rich():
            self._rich_table(headers, grid)
            return
        widths = [max(len(h), *(len(line[i]) for line in grid), 0) for i, h in enumerate(headers)]
        for line in (headers, *grid):
            print(
                "  ".join(c.ljust(w) for c, w in zip(line, widths, strict=True)).rstrip(),
                file=self.stdout,
            )

    def pairs(self, items: Sequence[tuple[str, Any]]) -> None:
        """Name and value per line."""
        rows = [{"name": name, "value": value} for name, value in items]
        self.table(rows, [Column("field", "name"), Column("value", "value")], truncate=False)

    def _use_rich(self) -> bool:
        if self._stdout is not None or not sys.stdout.isatty():
            return False
        try:
            import rich  # noqa: F401
        except ImportError:  # pragma: no cover - rich ships with typer
            return False
        return True

    def _rich_table(self, headers: list[str], grid: list[list[str]]) -> None:  # pragma: no cover
        from rich.console import Console
        from rich.table import Table

        table = Table(show_header=True, header_style="bold")
        for header in headers:
            table.add_column(header, overflow="fold")
        for line in grid:
            table.add_row(*line)
        Console().print(table)
