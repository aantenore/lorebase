"""Options shared by many commands, and the shell completion of Domain ids."""

from __future__ import annotations

import os
from typing import Annotated

import typer

from lorebase_cli.context import default_client
from lorebase_cli.settings import load_settings


def complete_domain(incomplete: str) -> list[str]:
    """Domain ids starting with `incomplete`, from the configured server (none on any failure,
    since completion must never print errors)."""
    try:
        settings = load_settings(env=os.environ)
        if not settings.url:
            return []
        client = default_client(settings)
        try:
            return [d.id for d in client.domains.iterate() if d.id.startswith(incomplete)]
        finally:
            client.close()
    except (Exception, SystemExit):
        return []


JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")]
DomainOpt = Annotated[
    str | None,
    typer.Option(
        "--domain",
        "-d",
        help="Domain id (default: LOREBASE_DOMAIN or `domain` in the config file).",
        autocompletion=complete_domain,
    ),
]
BranchOpt = Annotated[
    str | None,
    typer.Option(
        "--branch",
        "-b",
        help="Branch (default: LOREBASE_BRANCH, `branch` in the config file, else the Domain's "
        "default branch).",
    ),
]
AtOpt = Annotated[
    str | None, typer.Option("--at", help="Revision id or tag to read at (time travel).")
]
LimitOpt = Annotated[
    int | None,
    typer.Option("--limit", "-n", min=1, help="Most items to print (default: list_limit setting)."),
]
AllOpt = Annotated[bool, typer.Option("--all", help="Print every item, however many.")]
MineOpt = Annotated[
    bool, typer.Option("--mine", help="Only items assigned to you through a steward role.")
]
