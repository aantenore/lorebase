"""`proposal create|list|get|review|merge|close|conflicts|resolve`: Proposals between branches."""

from __future__ import annotations

from typing import Annotated, Any, cast, get_args

import typer

from lorebase_cli.commands.data import json_argument
from lorebase_cli.context import AppContext, app_of
from lorebase_cli.failures import CliError
from lorebase_cli.options import AllOpt, DomainOpt, JsonOpt, LimitOpt
from lorebase_cli.output import Column, short_id
from lorebase_client import models as m

proposal_app = typer.Typer(
    help="Propose, review and merge changes between branches.", no_args_is_help=True
)

ProposalArg = Annotated[str, typer.Argument(help="Proposal id.", metavar="ID")]

PROPOSAL_COLUMNS = [
    Column("id", "id"),
    Column("title", "title"),
    Column("source", "source_ref"),
    Column("target", "target_ref"),
    Column("status", "status"),
    Column("author", "author"),
    Column("created", "created_at"),
]
CONFLICT_COLUMNS = [
    Column("id", "id", short_id),
    Column("status", "status"),
    Column("kind", "kind"),
    Column("where", "partition"),
    Column("key", "key"),
    Column("message", "message"),
]


def _show_proposal(app: AppContext, found: m.ProposalDetail) -> None:
    approval = found.approval
    pairs: list[tuple[str, Any]] = [
        ("proposal", found.id),
        ("title", found.title),
        ("description", found.description),
        ("branches", f"{found.source_ref} -> {found.target_ref}"),
        ("origin", found.origin),
        ("status", found.status),
        ("author", found.author),
        ("created", found.created_at),
        ("updated", found.updated_at),
        ("merged revision", found.merged_revision),
        ("checks", [f"{c.name}: {c.status}" for c in found.checks]),
        (
            "approvals",
            f"{approval.approvals}/{approval.required}"
            + (" (satisfied)" if approval.satisfied else ""),
        ),
        ("reviews", len(found.reviews)),
    ]
    if found.attempt is not None:
        pairs.append(
            (
                "conflicts",
                f"{found.attempt.open_conflicts} open, {found.attempt.resolved_conflicts} resolved",
            )
        )
    app.out.pairs(pairs)


@proposal_app.command("create")
def proposal_create(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Branch with the changes.", metavar="SOURCE")],
    target: Annotated[str, typer.Argument(help="Branch to merge into.", metavar="TARGET")],
    title: Annotated[str, typer.Option("--title", "-t", help="Title of the Proposal.")],
    description: Annotated[
        str | None, typer.Option("--description", help="Longer description.")
    ] = None,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Open a Proposal to merge SOURCE into TARGET."""
    app = app_of(ctx, json_out)
    created = app.client().proposals.create(
        app.domain(domain), source, target, title=title, description=description
    )
    app.out.emit(created, lambda: _show_proposal(app, created))


@proposal_app.command("list")
def proposal_list(
    ctx: typer.Context,
    status: Annotated[str | None, typer.Option("--status", help="open, merged or closed.")] = None,
    source: Annotated[
        str | None, typer.Option("--source", help="Only Proposals from this branch.")
    ] = None,
    target: Annotated[
        str | None, typer.Option("--target", help="Only Proposals into this branch.")
    ] = None,
    author: Annotated[
        str | None, typer.Option("--author", help="Only Proposals by this principal.")
    ] = None,
    domain: DomainOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the Proposals of a Domain."""
    app = app_of(ctx, json_out)
    items, more = app.take(
        app.client().proposals.iterate(
            app.domain(domain),
            status=cast("m.ProposalStatus | None", status),
            source=source,
            target=target,
            author=author,
        ),
        limit,
        everything,
    )
    app.out.emit(items, lambda: app.out.table([p.model_dump() for p in items], PROPOSAL_COLUMNS))
    if more:
        app.note_truncated(len(items))


@proposal_app.command("get")
def proposal_get(
    ctx: typer.Context,
    proposal_id: ProposalArg,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Show a Proposal with its checks, reviews and approval state."""
    app = app_of(ctx, json_out)
    found = app.client().proposals.get(app.domain(domain), proposal_id)
    app.out.emit(found, lambda: _show_proposal(app, found))


@proposal_app.command("merge")
def proposal_merge(
    ctx: typer.Context,
    proposal_id: ProposalArg,
    message: Annotated[
        str | None, typer.Option("--message", "-m", help="Message of the merge Revision.")
    ] = None,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Merge a Proposal. Open Conflicts or missing approvals refuse it with exit code 2."""
    app = app_of(ctx, json_out)
    client = app.client()
    merged = client.proposals.merge(
        app.domain(domain), proposal_id, message=message, idempotency_key=client.idempotency_key()
    )
    app.out.emit(
        merged,
        lambda: app.out.pairs(
            [
                ("proposal", merged.proposal.id),
                ("branches", f"{merged.proposal.source_ref} -> {merged.proposal.target_ref}"),
                ("result", merged.result),
                ("revision", merged.revision),
                ("source deleted", merged.source_deleted),
                ("replayed", merged.replayed),
            ]
        ),
    )


@proposal_app.command("review")
def proposal_review(
    ctx: typer.Context,
    proposal_id: ProposalArg,
    verdict: Annotated[
        str, typer.Argument(help="approve, request_changes or comment.", metavar="VERDICT")
    ],
    comment: Annotated[str | None, typer.Option("--comment", "-c", help="Review comment.")] = None,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Review a Proposal; approvals count towards merging into a protected branch."""
    if verdict not in get_args(m.ReviewVerdict):
        raise CliError(f"unknown verdict {verdict!r}: use approve, request_changes or comment")
    app = app_of(ctx, json_out)
    result = app.client().proposals.review(
        app.domain(domain), proposal_id, cast("m.ReviewVerdict", verdict), comment=comment
    )
    approval = result.approval
    app.out.emit(
        result,
        lambda: app.out.pairs(
            [
                ("proposal", proposal_id),
                ("verdict", verdict),
                (
                    "approvals",
                    f"{approval.approvals}/{approval.required}"
                    + (" (satisfied)" if approval.satisfied else ""),
                ),
            ]
        ),
    )


@proposal_app.command("close")
def proposal_close(
    ctx: typer.Context,
    proposal_id: ProposalArg,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Close a Proposal without merging it."""
    app = app_of(ctx, json_out)
    closed = app.client().proposals.close(app.domain(domain), proposal_id)
    app.out.emit(closed, lambda: _show_proposal(app, closed))


@proposal_app.command("conflicts")
def proposal_conflicts(
    ctx: typer.Context,
    proposal_id: ProposalArg,
    status: Annotated[
        str | None, typer.Option("--status", help="open or resolved (default: both).")
    ] = None,
    domain: DomainOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the Conflicts of the latest merge attempt of a Proposal."""
    app = app_of(ctx, json_out)
    items, more = app.take(
        app.client().proposals.iterate_conflicts(
            app.domain(domain), proposal_id, status=cast("m.ConflictStatus | None", status)
        ),
        limit,
        everything,
    )
    app.out.emit(items, lambda: app.out.table([c.model_dump() for c in items], CONFLICT_COLUMNS))
    if more:
        app.note_truncated(len(items))


@proposal_app.command("resolve")
def proposal_resolve(
    ctx: typer.Context,
    proposal_id: ProposalArg,
    conflict_id: Annotated[
        str | None,
        typer.Argument(help="Conflict id (see `proposal conflicts`).", metavar="[CONFLICT_ID]"),
    ] = None,
    choice: Annotated[
        str | None,
        typer.Option("--choice", help="ours, theirs, base, custom, drop, restore or relax."),
    ] = None,
    value: Annotated[
        str | None,
        typer.Option("--value", help="Value of a `custom` choice: inline JSON, @file or -."),
    ] = None,
    file: Annotated[
        str | None,
        typer.Option(
            "--file",
            help='Resolve many Conflicts: a JSON array (or {"resolutions": [...]}) of '
            "objects with `fingerprint`, `choice` and optionally `value` or `values`.",
        ),
    ] = None,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Decide one Conflict (CONFLICT_ID and --choice), or many with --file.

    Deciding one only stores the decision; --file also re-runs the merge and prints the new
    attempt.
    """
    app = app_of(ctx, json_out)
    name = app.domain(domain)
    if file is not None:
        if conflict_id or choice or value:
            raise CliError("--file replaces CONFLICT_ID, --choice and --value")
        attempt = app.client().proposals.resolve_many(name, proposal_id, _resolutions(file))
        app.out.emit(
            attempt,
            lambda: app.out.pairs(
                [
                    ("proposal", attempt.proposal),
                    ("status", attempt.status),
                    ("open conflicts", attempt.open_conflicts),
                    ("resolved conflicts", attempt.resolved_conflicts),
                ]
            ),
        )
        return
    if not conflict_id or not choice:
        raise CliError("give CONFLICT_ID and --choice, or --file")
    resolution = app.client().proposals.resolve(
        name,
        proposal_id,
        conflict_id,
        choice,
        value=None if value is None else json_argument(value),
    )
    app.out.emit(
        resolution,
        lambda: app.out.pairs(
            [
                ("conflict", conflict_id),
                ("choice", resolution.choice),
                ("value", resolution.value),
                ("resolver", resolution.resolver),
                ("at", resolution.at),
            ]
        ),
    )


def _resolutions(file: str) -> list[dict[str, Any]]:
    parsed = json_argument(file if file == "-" else "@" + file)
    if isinstance(parsed, dict):
        parsed = parsed.get("resolutions")
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise CliError("the resolutions file is a JSON array of objects")
    return parsed
