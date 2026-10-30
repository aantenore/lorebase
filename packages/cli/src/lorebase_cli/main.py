"""The `lorebase` command line application."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

import lorebase_client
from lorebase_cli import __version__
from lorebase_cli.commands import (
    admin,
    agents,
    audit,
    data,
    documents,
    domains,
    history,
    proposals,
    reconcile,
    schema,
    sources,
    trace,
)
from lorebase_cli.context import AppContext
from lorebase_cli.failures import EXIT_USAGE, CliError, render_failure
from lorebase_cli.settings import ENV_CONFIG, ENV_PROFILE, load_settings
from lorebase_client import LorebaseError

CLICK_USAGE_EXIT = 2  # exit code Typer gives to usage errors; ours is EXIT_USAGE

EPILOG = (
    "Exit codes: 0 success, 1 usage or configuration error (also not found), "
    "2 validation failure or conflict, 3 authentication or permission failure, "
    "4 server error or server unreachable."
)


def _is_invalid_request(error: Exception) -> bool:
    """A request model of the client rejected the arguments (a pydantic validation error)."""
    return type(error).__module__.startswith("pydantic") and isinstance(error, ValueError)


class LorebaseGroup(TyperGroup):
    """Turns failures into messages on standard error and the documented exit codes."""

    def invoke(self, ctx: Any) -> Any:  # Typer's vendored Context class is not public
        try:
            return super().invoke(ctx)
        except typer.TyperException as error:  # usage errors: bad option, missing argument
            show = getattr(error, "show", None)
            if callable(show):
                show()
            else:  # pragma: no cover - every TyperException of typer 0.27 can show itself
                typer.echo(f"error: {error}", err=True)
            code = getattr(error, "exit_code", EXIT_USAGE)
            raise typer.Exit(EXIT_USAGE if code == CLICK_USAGE_EXIT else code) from None
        except (LorebaseError, CliError) as error:
            app = ctx.find_object(AppContext)
            text, code = render_failure(error, json_mode=bool(app and app.json_mode))
            typer.echo(text, err=True)
            raise typer.Exit(code) from None
        except ValueError as error:
            if not _is_invalid_request(error):
                raise
            typer.echo(f"error: invalid arguments\n{error}", err=True)
            raise typer.Exit(EXIT_USAGE) from None


app = typer.Typer(
    cls=LorebaseGroup,
    name="lorebase",
    help="Command line client of a Lorebase Keeper.\n\n"
    "Point it at a server with LOREBASE_URL and LOREBASE_API_KEY (or LOREBASE_TOKEN), or a "
    "profile in ~/.config/lorebase/config.toml. Every command takes --json for machine output. "
    "Run `lorebase --install-completion` for shell completion.",
    epilog=EPILOG,
    no_args_is_help=True,
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version(show: bool) -> None:
    if show:
        typer.echo(f"lorebase {__version__} (client {lorebase_client.__version__})")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", help=f"Config file (default: ~/.config/lorebase/config.toml; {ENV_CONFIG})."
        ),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help=f"Profile of the config file ({ENV_PROFILE}).")
    ] = None,
    url: Annotated[str | None, typer.Option("--url", help="Server URL (LOREBASE_URL).")] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of tables, for every command.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Print no progress or notes.")
    ] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    """Load the settings every command uses."""
    del version
    settings = load_settings(
        env=os.environ,
        config_path=config,
        profile=profile,
        url=url,
        warn=lambda message: typer.echo(f"warning: {message}", err=True),
    )
    state = AppContext(settings=settings, json_mode=json_out, quiet=quiet)
    ctx.obj = state
    ctx.call_on_close(state.close)


app.add_typer(domains.domain_app, name="domain")
app.add_typer(domains.branch_app, name="branch")
app.add_typer(domains.tag_app, name="tag")
app.add_typer(schema.schema_app, name="schema")
app.add_typer(data.import_app, name="import")
app.add_typer(data.record_app, name="record")
app.add_typer(history.revision_app, name="revision")
app.add_typer(admin.apikey_app, name="apikey")
app.add_typer(admin.admin_app, name="admin")
app.add_typer(audit.audit_app, name="audit")
app.add_typer(trace.trace_app, name="trace")
app.add_typer(proposals.proposal_app, name="proposal")
app.add_typer(reconcile.reconcile_app, name="reconcile")
app.add_typer(sources.sources_app, name="sources")
app.add_typer(documents.documents_app, name="documents")
app.command("whoami")(domains.whoami)
app.command("query")(data.query_command)
app.command("search")(data.search_command)
app.command("resolve")(data.resolve_command)
app.command("log")(history.log_command)
app.command("diff")(history.diff_command)
app.command("context")(agents.context_command)
app.command("explain")(agents.explain_command)
app.command("health")(agents.health_command)


def main() -> None:
    """Entry point of the `lorebase` script."""
    app(prog_name="lorebase")
