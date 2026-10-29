"""`reconcile list|show|resolve|dismiss|reopen`: the reconciliation queue."""

from __future__ import annotations

from typing import Annotated, Any, cast, get_args

import typer

from lorebase_cli.commands.data import json_argument
from lorebase_cli.context import AppContext, app_of
from lorebase_cli.failures import CliError
from lorebase_cli.options import AllOpt, BranchOpt, DomainOpt, JsonOpt, LimitOpt, MineOpt
from lorebase_cli.output import Column
from lorebase_client import models as m

reconcile_app = typer.Typer(
    help="Review contradictions and duplicates between Sources.", no_args_is_help=True
)

CandidateArg = Annotated[str, typer.Argument(help="Candidate id.", metavar="ID")]
ReasonOpt = Annotated[str, typer.Option("--reason", "-r", help="Why; kept in the audit log.")]

CANDIDATE_COLUMNS = [
    Column("id", "id"),
    Column("type", "type"),
    Column("status", "status"),
    Column("kind", "kind"),
    Column("keys", "keys", lambda keys: ", ".join(keys)),
    Column("attribute", "attribute"),
    Column("claims", "claim_count"),
    Column("stewards", "steward_roles"),
    Column("assignees", "assignees"),
    Column("created", "created_at"),
]


def _show_candidate(app: AppContext, found: m.ReconciliationCandidate) -> None:
    pairs: list[tuple[str, Any]] = [
        ("candidate", found.id),
        ("branch", found.branch),
        ("type", found.type),
        ("status", found.status),
        ("kind", found.kind),
        ("keys", found.keys),
        ("attribute", found.attribute),
        (
            "claims",
            [
                f"{c.claim_id}: {c.value!r}" + (f" ({c.source})" if c.source else "")
                for c in found.claims
            ],
        ),
        ("claim count", found.claim_count),
        ("proposal", found.proposal_id),
        ("decided by", found.decided_by),
        ("decided at", found.decided_at),
        ("created", found.created_at),
        ("updated", found.updated_at),
    ]
    app.out.pairs(pairs)


def _show_decision(app: AppContext, decided: m.ReconciliationDecision) -> None:
    app.out.pairs(
        [
            ("candidate", decided.candidate.id),
            ("status", decided.status),
            ("claim", decided.claim_id),
            ("branch", decided.branch),
            ("revision", decided.revision),
            ("proposal", decided.proposal),
            ("detail", decided.detail),
        ]
    )


def _checked(value: str | None, allowed: Any, option: str) -> str | None:
    """`value` checked against the members of a Literal type."""
    if value is not None and value not in get_args(allowed):
        raise CliError(f"{option}: unknown {value!r}, use {', '.join(get_args(allowed))}")
    return value


@reconcile_app.command("list")
def reconcile_list(
    ctx: typer.Context,
    candidate_type: Annotated[
        str | None,
        typer.Option("--type", help="duplicate, contradiction, superseded or stale."),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="open, resolved, dismissed or obsolete (default: all)."),
    ] = None,
    kind: Annotated[str | None, typer.Option("--kind", help="Only this Kind (api name).")] = None,
    attribute: Annotated[
        str | None,
        typer.Option("--attribute", help="Only this Attribute (api name; needs --kind)."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Only candidates with a claim of this Source identity."),
    ] = None,
    mine: MineOpt = False,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the candidates of a branch, newest first."""
    chosen_type = _checked(candidate_type, m.CandidateType, "--type")
    chosen_status = _checked(status, m.CandidateStatus, "--status")
    app = app_of(ctx, json_out)
    items, more = app.take(
        app.client().reconciliation.iterate(
            app.domain(domain),
            branch=app.branch(branch),
            type=cast("m.CandidateType | None", chosen_type),
            status=cast("m.CandidateStatus | None", chosen_status),
            kind=kind,
            attribute=attribute,
            source=source,
            assigned="me" if mine else None,
        ),
        limit,
        everything,
    )
    app.out.emit(items, lambda: app.out.table([c.model_dump() for c in items], CANDIDATE_COLUMNS))
    if more:
        app.note_truncated(len(items))


@reconcile_app.command("show")
def reconcile_show(
    ctx: typer.Context,
    candidate_id: CandidateArg,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Show a candidate with its claims and last decision."""
    app = app_of(ctx, json_out)
    found = app.client().reconciliation.get(app.domain(domain), candidate_id)
    app.out.emit(found, lambda: _show_candidate(app, found))


@reconcile_app.command("resolve")
def reconcile_resolve(
    ctx: typer.Context,
    candidate_id: CandidateArg,
    choice: Annotated[
        str, typer.Option("--choice", help="claim, keep_current, custom, policy or merge.")
    ],
    reason: ReasonOpt,
    claim: Annotated[
        str | None, typer.Option("--claim", help="Claim id to take (choice `claim`).")
    ] = None,
    value: Annotated[
        str | None,
        typer.Option("--value", help="Value of a `custom` choice: inline JSON, @file or -."),
    ] = None,
    policy: Annotated[
        str | None,
        typer.Option(
            "--policy",
            help="authority, newest_wins, prefer_current or prefer_incoming (choice `policy`).",
        ),
    ] = None,
    survivor: Annotated[
        str | None, typer.Option("--survivor", help="Key of the Record that stays (`merge`).")
    ] = None,
    values: Annotated[
        str | None,
        typer.Option(
            "--values",
            help="Values to set on the survivor, by Attribute api name (`merge`): "
            "a JSON object, inline, @file or -.",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report the outcome without writing.")
    ] = False,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Decide a candidate; a write that needs review opens a Proposal."""
    chosen = _checked(choice, m.ReconcileChoice, "--choice")
    chosen_policy = _checked(policy, m.ReconcilePolicy, "--policy")
    parsed = None if values is None else json_argument(values)
    if parsed is not None and not isinstance(parsed, dict):
        raise CliError("--values is a JSON object")
    app = app_of(ctx, json_out)
    decided = app.client().reconciliation.resolve(
        app.domain(domain),
        candidate_id,
        cast("m.ReconcileChoice", chosen),
        reason=reason,
        claim_id=claim,
        value=None if value is None else json_argument(value),
        policy=cast("m.ReconcilePolicy | None", chosen_policy),
        survivor=survivor,
        values=parsed,
        dry_run=dry_run,
    )
    app.out.emit(decided, lambda: _show_decision(app, decided))


@reconcile_app.command("dismiss")
def reconcile_dismiss(
    ctx: typer.Context,
    candidate_id: CandidateArg,
    reason: ReasonOpt,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Dismiss a candidate: the difference is accepted as it is."""
    app = app_of(ctx, json_out)
    decided = app.client().reconciliation.dismiss(app.domain(domain), candidate_id, reason=reason)
    app.out.emit(decided, lambda: _show_decision(app, decided))


@reconcile_app.command("reopen")
def reconcile_reopen(
    ctx: typer.Context,
    candidate_id: CandidateArg,
    reason: ReasonOpt,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Reopen a dismissed candidate."""
    app = app_of(ctx, json_out)
    decided = app.client().reconciliation.reopen(app.domain(domain), candidate_id, reason=reason)
    app.out.emit(decided, lambda: _show_decision(app, decided))
