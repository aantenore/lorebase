"""`trace list`, `trace show`, `trace note`: agent trace sessions of a Keeper."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from lorebase_cli.context import app_of
from lorebase_cli.options import DomainOpt, JsonOpt, LimitOpt
from lorebase_cli.output import Column
from lorebase_client import models as m

trace_app = typer.Typer(help="Inspect what agents did in a session.", no_args_is_help=True)

SessionArg = Annotated[str, typer.Argument(help="Session id.", metavar="SESSION_ID")]
ActorOpt = Annotated[str | None, typer.Option("--actor", help="Only the calls of this principal.")]

SESSION_COLUMNS = [
    Column("session", "session_id"),
    Column("actor", "actor"),
    Column("last activity", "last_at"),
    Column("calls", "calls"),
    Column("notes", "notes"),
    Column("domains", "domains"),
    Column("tools", "tools"),
    Column("proposals", "proposals"),
    Column("inferred", "inferred"),
]
STEP_COLUMNS = [
    Column("step", "step"),
    Column("time", "at"),
    Column("type", "type"),
    Column("tool", "tool"),
    Column("endpoint", "endpoint"),
    Column("status", "status"),
    Column("ms", "latency_ms"),
    Column("cache", "cache"),
    Column("counts", "counts"),
    Column("refs", "refs"),
    Column("note", "note"),
]


def plain(text: str | None) -> str | None:
    """Opaque text for a terminal: control characters (escape sequences) are replaced."""
    if text is None:
        return None
    return "".join(c if c.isprintable() else "?" for c in text)


def counts_of(result: dict[str, Any] | None) -> str:
    """`items=3 edges=2` from the result counts of a call."""
    counts = (result or {}).get("counts")
    if not isinstance(counts, dict):
        return ""
    return " ".join(f"{name}={value}" for name, value in counts.items())


def step_row(step: m.TraceStep) -> dict[str, Any]:
    """A timeline step as table cells."""
    row = step.model_dump()
    row["endpoint"] = " ".join(part for part in (step.method, step.route) if part)
    row["counts"] = counts_of(step.result)
    refs = step.refs
    row["refs"] = (
        ""
        if refs is None
        else f"{len(refs.records)}r {len(refs.links)}l" + ("+" if refs.truncated else "")
    )
    row["latency_ms"] = None if step.latency_ms is None else f"{step.latency_ms:.0f}"
    row["note"] = plain(step.note)
    return row


@trace_app.command("list")
def trace_list(
    ctx: typer.Context,
    actor: ActorOpt = None,
    domain: DomainOpt = None,
    limit: LimitOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """List the sessions you may see, most recent activity first."""
    app = app_of(ctx, json_out)
    page = app.client().trace.sessions(
        actor=actor, domain_id=domain, limit=limit or app.settings.list_limit
    )

    def human() -> None:
        if not page.enabled:
            app.out.info("tracing is off on this server; showing recorded sessions only")
        app.out.table([s.model_dump() for s in page.items], SESSION_COLUMNS)

    app.out.emit(page, human)
    if page.next_cursor is not None:
        app.note_truncated(len(page.items))


@trace_app.command("show")
def trace_show(
    ctx: typer.Context,
    session_id: SessionArg,
    actor: ActorOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Print the timeline of a session: calls with their outcome, and notes."""
    app = app_of(ctx, json_out)
    timeline = app.client().trace.session(session_id, actor=actor)

    def human() -> None:
        if any(s.inferred for s in timeline.steps):
            app.out.info("inferred session: calls without a session id, grouped heuristically")
        app.out.table([step_row(s) for s in timeline.steps], STEP_COLUMNS)
        if timeline.truncated:
            app.out.info("the session has more steps than shown")

    app.out.emit(timeline, human)


@trace_app.command("note")
def trace_note(
    ctx: typer.Context,
    session_id: SessionArg,
    text: Annotated[str, typer.Argument(help="Text of the note.", metavar="TEXT")],
    domain: DomainOpt = None,
    step: Annotated[
        int | None, typer.Option("--step", min=0, help="Step counter of the agent.")
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """Attach a note to a session (written off the request path)."""
    app = app_of(ctx, json_out)
    result = app.client().trace.add_note(session_id, text, domain_id=domain, step=step)
    app.out.emit(
        result,
        lambda: app.out.info(
            "note recorded" if result.recorded else "note dropped: tracing is off"
        ),
    )
