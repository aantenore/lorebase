"""`whoami`, `domain`, `branch` and `tag` commands."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from lorebase_cli.context import AppContext, app_of
from lorebase_cli.failures import CliError
from lorebase_cli.options import AllOpt, DomainOpt, JsonOpt, LimitOpt, complete_domain
from lorebase_cli.output import Column, short_id
from lorebase_client.models import Domain

domain_app = typer.Typer(help="Create, inspect and delete Domains.", no_args_is_help=True)
branch_app = typer.Typer(help="List, create, protect and delete branches.", no_args_is_help=True)
tag_app = typer.Typer(
    help="List and create tags (immutable names for Revisions).", no_args_is_help=True
)

DomainId = Annotated[
    str, typer.Argument(help="Domain id.", metavar="DOMAIN", autocompletion=complete_domain)
]

DOMAIN_COLUMNS = [
    Column("id", "id"),
    Column("name", "display_name"),
    Column("default branch", "default_branch"),
    Column("created", "created_at"),
]


def whoami(ctx: typer.Context, json_out: JsonOpt = False) -> None:
    """Show who the credentials authenticate as."""
    app = app_of(ctx, json_out)
    me = app.client().whoami()
    app.out.emit(
        me,
        lambda: app.out.pairs(
            [
                ("id", me.id),
                ("name", me.display_name),
                ("type", me.type),
                ("method", me.method),
                ("installation admin", me.installation_admin),
                ("subjects", me.subjects),
            ]
        ),
    )


# Domains ----------------------------------------------------------------------------------


@domain_app.command("create")
def domain_create(
    ctx: typer.Context,
    domain_id: Annotated[
        str, typer.Argument(help="Domain id: lowercase slug, immutable.", metavar="DOMAIN")
    ],
    name: Annotated[str, typer.Option("--name", help="Display name.")] = "",
    description: Annotated[str, typer.Option("--description", help="Description.")] = "",
    default_branch: Annotated[
        str | None, typer.Option(help="Name of the default branch (server default: main).")
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """Create a Domain with an empty first Revision and a protected default branch."""
    app = app_of(ctx, json_out)
    created = app.client().domains.create(
        domain_id, display_name=name, description=description, default_branch=default_branch
    )
    app.out.emit(created, lambda: _show_domain(app, created))


@domain_app.command("list")
def domain_list(
    ctx: typer.Context,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the Domains you can read."""
    app = app_of(ctx, json_out)
    items, more = app.take(app.client().domains.iterate(), limit, everything)
    app.out.emit(items, lambda: app.out.table([d.model_dump() for d in items], DOMAIN_COLUMNS))
    if more:
        app.note_truncated(len(items))


@domain_app.command("get")
def domain_get(ctx: typer.Context, domain_id: DomainId, json_out: JsonOpt = False) -> None:
    """Show a Domain and the head of its default branch."""
    app = app_of(ctx, json_out)
    found = app.client().domains.get(domain_id)
    app.out.emit(found, lambda: _show_domain(app, found))


@domain_app.command("delete")
def domain_delete(
    ctx: typer.Context,
    domain_id: DomainId,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    json_out: JsonOpt = False,
) -> None:
    """Delete a Domain with all its Revisions, branches and grants. This cannot be undone."""
    app = app_of(ctx, json_out)
    if not yes:
        if not _interactive():
            raise CliError("deleting a Domain is irreversible: pass --yes to confirm")
        typed = typer.prompt(f"This deletes Domain {domain_id!r} for good. Type its id to confirm")
        if typed != domain_id:
            raise CliError("the id typed does not match; nothing was deleted")
    app.client().domains.delete(domain_id, confirm=domain_id)
    app.out.emit({"deleted": domain_id}, lambda: app.out.info(f"deleted Domain {domain_id}"))


def _interactive() -> bool:
    return sys.stdin.isatty()


def _show_domain(app: AppContext, domain: Domain) -> None:
    app.out.pairs(
        [
            ("id", domain.id),
            ("name", domain.display_name),
            ("description", domain.description),
            ("default branch", domain.default_branch),
            ("head", domain.head),
            ("created", domain.created_at),
            ("created by", domain.created_by),
        ]
    )


# Branches ---------------------------------------------------------------------------------

BRANCH_COLUMNS = [
    Column("branch", "name"),
    Column("head", "head", short_id),
    Column("protected", "protected"),
    Column("default", "default"),
    Column("materialization", "materialization"),
]


@branch_app.command("list")
def branch_list(
    ctx: typer.Context,
    domain: DomainOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the branches of a Domain."""
    app = app_of(ctx, json_out)
    items, more = app.take(app.client().branches.iterate(app.domain(domain)), limit, everything)
    app.out.emit(items, lambda: app.out.table([b.model_dump() for b in items], BRANCH_COLUMNS))
    if more:
        app.note_truncated(len(items))


@branch_app.command("get")
def branch_get(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Branch name.", metavar="NAME")],
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Show a branch and how far it is from the default branch."""
    app = app_of(ctx, json_out)
    found = app.client().branches.get(app.domain(domain), name)
    app.out.emit(
        found,
        lambda: app.out.pairs(
            [
                ("branch", found.name),
                ("head", found.head),
                ("protected", found.protected),
                ("default", found.default),
                ("ahead", found.ahead),
                ("behind", found.behind),
                ("materialization", found.materialization),
                ("created", found.created_at),
            ]
        ),
    )


@branch_app.command("create")
def branch_create(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Argument(help="New branch name (may contain slashes).", metavar="NAME")
    ],
    source: Annotated[
        str | None, typer.Option("--source", help="Branch or tag to start from (default branch).")
    ] = None,
    revision: Annotated[
        str | None, typer.Option("--revision", help="Revision id to start at.")
    ] = None,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Create a branch from a branch, a tag or a Revision."""
    app = app_of(ctx, json_out)
    created = app.client().branches.create(
        app.domain(domain), name, source=source, revision=revision
    )
    app.out.emit(created, lambda: app.out.table([created.model_dump()], BRANCH_COLUMNS))


@branch_app.command("delete")
def branch_delete(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Branch name.", metavar="NAME")],
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Delete a branch (its Revisions stay reachable by id and tag). The default and
    protected branches cannot be deleted."""
    app = app_of(ctx, json_out)
    app.client().branches.delete(app.domain(domain), name)
    app.out.emit({"deleted": name}, lambda: app.out.info(f"deleted branch {name}"))


@branch_app.command("protect")
def branch_protect(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Branch name.", metavar="NAME")],
    off: Annotated[bool, typer.Option("--off", help="Remove the protection instead.")] = False,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Protect a branch: changes reach it through Proposals only."""
    app = app_of(ctx, json_out)
    updated = app.client().branches.set_protected(app.domain(domain), name, not off)
    app.out.emit(updated, lambda: app.out.table([updated.model_dump()], BRANCH_COLUMNS))


# Tags -------------------------------------------------------------------------------------

TAG_COLUMNS = [
    Column("tag", "name"),
    Column("revision", "revision", short_id),
    Column("created", "created_at"),
    Column("by", "created_by"),
]


@tag_app.command("list")
def tag_list(
    ctx: typer.Context,
    domain: DomainOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the tags of a Domain."""
    app = app_of(ctx, json_out)
    items, more = app.take(app.client().tags.iterate(app.domain(domain)), limit, everything)
    app.out.emit(items, lambda: app.out.table([t.model_dump() for t in items], TAG_COLUMNS))
    if more:
        app.note_truncated(len(items))


@tag_app.command("create")
def tag_create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Tag name.", metavar="NAME")],
    source: Annotated[
        str | None, typer.Option("--source", help="Branch whose head to tag.")
    ] = None,
    revision: Annotated[str | None, typer.Option("--revision", help="Revision id to tag.")] = None,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Tag a Revision (default: the head of the default branch). Tags never move."""
    app = app_of(ctx, json_out)
    created = app.client().tags.create(app.domain(domain), name, source=source, revision=revision)
    app.out.emit(created, lambda: app.out.table([created.model_dump()], TAG_COLUMNS))
