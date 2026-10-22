"""`audit verify`: check the hash chain of a Domain's audit log."""

from __future__ import annotations

from typing import Annotated

import typer

from lorebase_cli.context import app_of
from lorebase_cli.failures import EXIT_INVALID
from lorebase_cli.options import DomainOpt, JsonOpt

audit_app = typer.Typer(help="Check the audit log of a Domain.", no_args_is_help=True)


@audit_app.command("verify")
def audit_verify(
    ctx: typer.Context,
    domain: DomainOpt = None,
    after_seq: Annotated[
        int | None,
        typer.Option(
            "--after-seq",
            min=0,
            help="Start after this entry, trusting it (the last seq of an earlier run).",
        ),
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """Recompute the audit hash chain of a Domain and report the first broken entry.

    Needs the audit.read permission (the admin role). Exits with 2 when an entry does not
    verify.
    """
    app = app_of(ctx, json_out)
    result = app.client().audit.verify(app.domain(domain), after_seq=after_seq)

    def human() -> None:
        if result.first_broken is None:
            app.out.text(f"intact: {result.checked} entries verified")
        else:
            broken = result.first_broken
            app.out.text(
                f"broken at seq {broken.seq}: {broken.reason}; "
                f"{result.checked} entries verified before it"
            )
        app.out.pairs([("domain", result.domain_id), ("last seq", result.last_seq)])

    app.out.emit(result, human)
    if not result.intact:
        raise typer.Exit(EXIT_INVALID)
