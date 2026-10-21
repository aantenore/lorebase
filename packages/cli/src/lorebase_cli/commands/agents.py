"""`context`, `explain` and `health`: what an agent reads about Records, where their values came
from, and how sound the data is."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from lorebase_cli.commands.data import json_argument
from lorebase_cli.context import app_of
from lorebase_cli.failures import CliError
from lorebase_cli.options import BranchOpt, DomainOpt, JsonOpt
from lorebase_cli.output import Column, short_id
from lorebase_client import models as m

MarkdownOpt = Annotated[
    bool, typer.Option("--markdown", help="Print the Markdown rendering instead of a summary.")
]

SEED_COLUMNS = [Column("record", "ref"), Column("title", "title"), Column("values", "values")]
NEIGHBOR_COLUMNS = [
    Column("record", "ref"),
    Column("title", "title"),
    Column("depth", "depth"),
]
ATTRIBUTE_COLUMNS = [
    Column("attribute", "name"),
    Column("value", "value"),
    Column("actor", "actor"),
    Column("origin", "origin_type"),
    Column("revision", "revision", short_id),
    Column("at", "at"),
]


def context_command(
    ctx: typer.Context,
    ref: Annotated[
        list[str] | None,
        typer.Option("--ref", help="Seed Record as KIND/KEY. Repeatable."),
    ] = None,
    search: Annotated[
        str | None, typer.Option("--search", help="Seed Records found by this text.")
    ] = None,
    query: Annotated[
        str | None,
        typer.Option("--query", help="Seed Records of a DSL query: inline JSON, @file or -."),
    ] = None,
    budget: Annotated[
        int | None, typer.Option("--budget", min=1, help="Token budget of the pack.")
    ] = None,
    depth: Annotated[
        int | None, typer.Option("--depth", min=0, help="Hops to expand around the seeds.")
    ] = None,
    relation: Annotated[
        list[str] | None,
        typer.Option("--relation", help="Follow only this Relation. Repeatable."),
    ] = None,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    markdown: MarkdownOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Build a context pack: seed Records, their neighbors, schema and recent changes.

    Name the seeds with exactly one of --ref, --search or --query.
    """
    app = app_of(ctx, json_out)
    parsed = None if query is None else json_argument(query)
    if parsed is not None and not isinstance(parsed, dict):
        raise CliError("a query is a JSON object")
    request = m.ContextRequest(
        branch=app.branch(branch),
        refs=ref or None,
        query=parsed,
        search=search,
        budget_tokens=budget,
        depth=depth,
        relations=relation or None,
    )
    agents = app.client().agents
    name = app.domain(domain)
    if markdown:
        app.out.text(agents.context_markdown(name, request))
        return
    pack = agents.context(name, request)

    def human() -> None:
        used = pack.budget.get("used_tokens")
        limit = pack.budget.get("limit_tokens")
        app.out.pairs(
            [
                ("domain", pack.domain),
                ("branch", pack.branch),
                ("revision", short_id(pack.revision)),
                ("seeds", len(pack.seeds)),
                ("neighbors", len(pack.neighbors)),
                ("tokens", f"{used}/{limit}"),
            ]
        )
        app.out.table(pack.seeds, SEED_COLUMNS)
        if pack.neighbors:
            app.out.table(pack.neighbors, NEIGHBOR_COLUMNS)

    app.out.emit(pack, human)


def explain_command(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Kind api name.", metavar="KIND")],
    key: Annotated[str, typer.Argument(help="Record key.", metavar="KEY")],
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    markdown: MarkdownOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Explain a Record: where each value and Link came from, and the Proposals behind it."""
    app = app_of(ctx, json_out)
    agents = app.client().agents
    name = app.domain(domain)
    chosen = app.branch(branch)
    if markdown:
        app.out.text(agents.explain_markdown(name, kind, key, branch=chosen))
        return
    found = agents.explain(name, kind, key, branch=chosen)

    def human() -> None:
        record = found.record
        app.out.pairs(
            [
                ("record", record.get("ref")),
                ("title", record.get("title")),
                ("branch", found.branch),
                ("revision", short_id(found.revision)),
                ("created by", (found.created or {}).get("actor")),
                ("links", len(found.links)),
                ("changes", found.history.get("changes")),
                ("proposals", [f"{p.get('id')} ({p.get('status')})" for p in found.proposals]),
            ]
        )
        app.out.table(found.attributes, ATTRIBUTE_COLUMNS)

    app.out.emit(found, human)


KIND_HEALTH_COLUMNS = [
    Column("kind", "kind"),
    Column("records", "records"),
    Column("invalid", "invalid"),
    Column("duplicate groups", "duplicates"),
    Column("derived", "derived"),
    Column("last change", "last_change", short_id),
]
RELATION_HEALTH_COLUMNS = [
    Column("relation", "relation"),
    Column("from", "from_kind"),
    Column("to", "to_kind"),
    Column("links", "links"),
    Column("orphans", "orphans"),
    Column("gaps", "gaps"),
]


def _kind_rows(kinds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": k["kind"],
            "records": k["records"],
            "invalid": k["violations"]["invalid_records"],
            "duplicates": k["duplicates"]["total_groups"],
            "derived": "stale" if (k.get("derived") or {}).get("stale") else "",
            "last_change": (k.get("last_change") or {}).get("revision"),
        }
        for k in kinds
    ]


def _relation_rows(relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **r,
            "orphans": r["orphan_links"]["count"],
            "gaps": None if r.get("required_gaps") is None else r["required_gaps"]["missing"],
        }
        for r in relations
    ]


def health_command(
    ctx: typer.Context,
    kind: Annotated[
        str | None,
        typer.Argument(help="Report on this Kind only.", metavar="[KIND]"),
    ] = None,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    markdown: MarkdownOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Check the data: constraint violations, orphan Links, required-Link gaps, duplicates."""
    app = app_of(ctx, json_out)
    agents = app.client().agents
    name = app.domain(domain)
    chosen = app.branch(branch)
    if markdown:
        app.out.text(
            agents.kind_health_markdown(name, kind, branch=chosen)
            if kind
            else agents.health_markdown(name, branch=chosen)
        )
        return
    found = (
        agents.kind_health(name, kind, branch=chosen)
        if kind
        else agents.health(name, branch=chosen)
    )
    kinds = [found.health] if isinstance(found, m.KindHealthReport) else found.kinds

    def human() -> None:
        summary = found.summary
        app.out.pairs(
            [
                ("domain", found.domain),
                ("branch", found.branch),
                ("revision", short_id(found.revision)),
                ("status", "ok" if summary.ok else "needs attention"),
                ("records", summary.records),
                ("invalid records", summary.invalid_records),
                ("orphan links", summary.orphan_links),
                ("required-link gaps", summary.required_relation_gaps),
                ("duplicate groups", summary.duplicate_groups),
                ("kinds with stale derived values", summary.stale_kinds),
            ]
        )
        app.out.table(_kind_rows(kinds), KIND_HEALTH_COLUMNS)
        if found.relations:
            app.out.table(_relation_rows(found.relations), RELATION_HEALTH_COLUMNS)

    app.out.emit(found, human)
