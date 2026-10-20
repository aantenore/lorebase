"""`log`, `diff` and `revision show`: history of a Domain."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Annotated, Any

import typer

from lorebase_cli.context import AppContext, app_of
from lorebase_cli.options import AllOpt, DomainOpt, JsonOpt, LimitOpt
from lorebase_cli.output import Column, short_id
from lorebase_client import endpoints as ep
from lorebase_client import models as m

revision_app = typer.Typer(help="Inspect Revisions.", no_args_is_help=True)

REVISION_COLUMNS = [
    Column("revision", "id", short_id),
    Column("gen", "generation"),
    Column("author", "author"),
    Column("origin", "origin", lambda o: (o or {}).get("type", "")),
    Column("message", "message", lambda v: str(v).splitlines()[0] if v else ""),
    Column("created", "created_at"),
]


def _revision_rows(items: list[m.Revision]) -> list[dict[str, Any]]:
    return [r.model_dump() for r in items]


def log_command(
    ctx: typer.Context,
    branch: Annotated[
        str | None,
        typer.Argument(
            help="Branch, tag or Revision (default: the default branch).", metavar="BRANCH"
        ),
    ] = None,
    domain: DomainOpt = None,
    author: Annotated[
        str | None, typer.Option("--author", help="Only Revisions by this principal.")
    ] = None,
    origin_type: Annotated[
        str | None,
        typer.Option(
            "--origin", help="Only Revisions of this origin (import, operation, merge, ...)."
        ),
    ] = None,
    all_parents: Annotated[
        bool, typer.Option("--all-parents", help="Follow every parent, not only the first.")
    ] = False,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Show the history of a branch, newest Revision first."""
    app = app_of(ctx, json_out)
    client = app.client()
    name = app.domain(domain)
    target = branch or app.branch(None) or client.domains.get(name).default_branch
    items, more = app.take(
        client.revisions.iterate_log(
            name, target, all_parents=all_parents, origin_type=origin_type, author=author
        ),
        limit,
        everything,
    )
    app.out.emit(items, lambda: app.out.table(_revision_rows(items), REVISION_COLUMNS))
    if more:
        app.note_truncated(len(items))


CHANGE_COLUMNS = [
    Column("change", "change"),
    Column("element", "element"),
    Column("where", "where"),
    Column("key", "key"),
    Column("values", "values"),
]


def change_row(change: m.Change) -> dict[str, Any]:
    """A change as one table row: Records and Links with the Attributes that changed."""
    where = change.kind or change.relation or change.element_type or ""
    key = change.key or ""
    if change.from_key is not None or change.to_key is not None:
        key = f"{change.from_key} -> {change.to_key}"
    if change.api_name and not key:
        key = change.api_name
    values = "; ".join(_value_change(v) for v in change.changes or [])
    return {
        "change": change.change,
        "element": change.element,
        "where": where,
        "key": key,
        "values": values,
    }


def _value_change(value: m.ValueChange) -> str:
    def show(item: object) -> str:
        return "(none)" if item is None else str(item)

    return f"{value.name}: {show(value.old)} -> {show(value.new)}"


def diff_command(
    ctx: typer.Context,
    from_: Annotated[
        str, typer.Argument(metavar="FROM", help="Older side: Revision id, tag or branch.")
    ],
    to: Annotated[
        str, typer.Argument(help="Newer side: Revision id, tag or branch.", metavar="TO")
    ],
    domain: DomainOpt = None,
    kind: Annotated[
        list[str] | None, typer.Option("--kind", help="Only this Kind. Repeatable.")
    ] = None,
    relation: Annotated[
        list[str] | None, typer.Option("--relation", help="Only this Relation. Repeatable.")
    ] = None,
    no_schema: Annotated[
        bool, typer.Option("--no-schema", help="Leave out schema changes.")
    ] = False,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Show what changed between two Revisions (a branch stands for its head)."""
    app = app_of(ctx, json_out)
    client = app.client()
    pages = client.pages(
        ep.diff(
            app.domain(domain),
            from_,
            to,
            kinds=kind,
            relations=relation,
            schema=False if no_schema else None,
        )
    )
    first = next(pages)
    items, more = app.take(itertools.chain(first.items, _items_of(pages)), limit, everything)
    document = {
        "from_revision": first.from_revision,
        "to_revision": first.to_revision,
        "total": first.total,
        "summary": first.summary,
        "items": items,
    }

    def human() -> None:
        app.out.table([change_row(c) for c in items], CHANGE_COLUMNS)
        summary = ", ".join(
            f"{s.name}: +{s.added} -{s.removed} ~{s.modified}" for s in first.summary
        )
        app.out.info(f"{first.total} changes ({summary})" if first.total else "no changes")

    app.out.emit(document, human)
    if more:
        app.note_truncated(len(items))


def _items_of(pages: Iterator[m.ChangesPage]) -> Iterator[m.Change]:
    for page in pages:
        yield from page.items


@revision_app.command("show")
def revision_show(
    ctx: typer.Context,
    revision: Annotated[
        str, typer.Argument(help="Revision id, tag or branch.", metavar="REVISION")
    ],
    domain: DomainOpt = None,
    changes: Annotated[
        bool, typer.Option("--changes", help="List what the Revision changed.")
    ] = False,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Show a Revision, and with --changes its changes against the first parent."""
    app = app_of(ctx, json_out)
    client = app.client()
    name = app.domain(domain)
    found = client.revisions.get(name, revision)
    listed: list[m.Change] = []
    more = False
    if changes:
        listed, more = app.take(
            client.revisions.iterate_changeset(name, found.id), limit, everything
        )

    def human() -> None:
        _show_revision(app, found)
        if changes:
            app.out.table([change_row(c) for c in listed], CHANGE_COLUMNS)

    payload: Any = {**found.model_dump(mode="json"), "changes": listed} if changes else found
    app.out.emit(payload, human)
    if more:
        app.note_truncated(len(listed))


def _show_revision(app: AppContext, found: m.Revision) -> None:
    app.out.pairs(
        [
            ("revision", found.id),
            ("parents", [short_id(p) for p in found.parents]),
            ("generation", found.generation),
            ("author", found.author),
            ("on behalf of", found.on_behalf_of),
            ("origin", found.origin),
            ("message", found.message),
            ("stats", found.stats),
            ("created", found.created_at),
        ]
    )
