"""`schema get|validate|apply|diff`: the schema of a Domain as a YAML document."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from lorebase_cli.context import AppContext, app_of
from lorebase_cli.failures import EXIT_INVALID, CliError
from lorebase_cli.options import AtOpt, BranchOpt, DomainOpt, JsonOpt
from lorebase_cli.output import Column
from lorebase_client import models as m

schema_app = typer.Typer(
    help="Read, check and change the schema of a Domain (YAML documents).", no_args_is_help=True
)

FileArg = Annotated[
    str, typer.Argument(help="Schema YAML document; `-` reads standard input.", metavar="FILE")
]
CHANGE_COLUMNS = [
    Column("change", "op"),
    Column("section", "section"),
    Column("element", "api_name"),
    Column("id", "id"),
]


def read_document(file: str) -> str:
    """Text of a file argument (`-` is standard input)."""
    if file == "-":
        return sys.stdin.read()
    try:
        return Path(file).read_text(encoding="utf-8")
    except OSError as error:
        raise CliError(f"cannot read {file}: {error.strerror or error}") from error


def _issue_lines(issues: list[dict[str, object]], count: int) -> list[str]:
    lines = []
    for issue in issues:
        path = ".".join(str(p) for p in issue.get("path", []) or [])  # type: ignore[attr-defined]
        lines.append(f"  {path}: {issue.get('message', '')} [{issue.get('code', '')}]")
    if count > len(issues):
        lines.append(f"  ... and {count - len(issues)} more")
    return lines


def _print_changes(app: AppContext, changes: list[m.ElementChange]) -> None:
    if changes:
        app.out.table([c.model_dump() for c in changes], CHANGE_COLUMNS)
    else:
        app.out.text("no changes")


def _default_branch(app: AppContext, domain: str, given: str | None) -> str:
    chosen = app.branch(given)
    return chosen or app.client().domains.get(domain).default_branch


@schema_app.command("get")
def schema_get(
    ctx: typer.Context,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    at: AtOpt = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write the document to this file.")
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """Print the schema as YAML (or as JSON with --json)."""
    app = app_of(ctx, json_out)
    client = app.client()
    name = app.domain(domain)
    if app.json_mode:
        document = client.schema.get(name, branch=app.branch(branch), at=at)
        if output:
            output.write_text(_as_json(document))
        else:
            app.out.data(document)
        return
    text = client.schema.get_yaml(name, branch=app.branch(branch), at=at)
    if output:
        output.write_text(text, encoding="utf-8")
        app.out.info(f"wrote {output}")
    else:
        app.out.text(text)


def _as_json(document: m.SchemaDocument) -> str:
    return json.dumps(document.model_dump(mode="json", by_alias=True), indent=2) + "\n"


@schema_app.command("validate")
def schema_validate(
    ctx: typer.Context,
    file: FileArg,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    at: AtOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Check a schema document against the schema of a branch. Nothing is written.

    Exits with 2 when the document is not valid.
    """
    app = app_of(ctx, json_out)
    result = app.client().schema.validate(
        app.domain(domain), yaml=read_document(file), branch=app.branch(branch), at=at
    )
    app.out.emit(result, lambda: _report_validation(app, result))
    if not result.valid:
        raise typer.Exit(EXIT_INVALID)


def _report_validation(app: AppContext, result: m.SchemaValidation) -> None:
    if result.valid:
        app.out.text(f"valid: {len(result.changes)} element changes")
        _print_changes(app, result.changes)
        return
    app.out.text(f"not valid: {result.issue_count} issue(s)")
    for line in _issue_lines(result.issues, result.issue_count):
        app.out.text(line)


@schema_app.command("diff")
def schema_diff(
    ctx: typer.Context,
    file: FileArg,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    at: AtOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Show which elements applying a schema document would create, update or delete."""
    app = app_of(ctx, json_out)
    result = app.client().schema.validate(
        app.domain(domain), yaml=read_document(file), branch=app.branch(branch), at=at
    )
    app.out.emit(result, lambda: _report_diff(app, result))
    if not result.valid:
        raise typer.Exit(EXIT_INVALID)


def _report_diff(app: AppContext, result: m.SchemaValidation) -> None:
    if not result.valid:
        _report_validation(app, result)
        return
    _print_changes(app, result.changes)


@schema_app.command("apply")
def schema_apply(
    ctx: typer.Context,
    file: FileArg,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    message: Annotated[
        str | None, typer.Option("--message", "-m", help="Revision message.")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan only; write nothing.")] = False,
    cascade: Annotated[
        bool,
        typer.Option("--cascade", help="Allow removing Kinds or Relations that hold data."),
    ] = False,
    expected_head: Annotated[
        str | None,
        typer.Option("--expected-head", help="Fail unless the branch is at this Revision."),
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """Apply a schema document to a branch as one Revision (data migrations included).

    Elements the document leaves out are removed. Protected branches (the default one) refuse
    direct changes: apply to another branch.
    """
    app = app_of(ctx, json_out)
    name = app.domain(domain)
    target = _default_branch(app, name, branch)
    result = app.client().schema.apply(
        name,
        target,
        yaml=read_document(file),
        message=message,
        expected_head=expected_head,
        dry_run=dry_run,
        cascade=cascade,
    )
    app.out.emit(result, lambda: _report_apply(app, result))


def _report_apply(app: AppContext, result: m.SchemaChangeResult) -> None:
    app.out.pairs(
        [
            ("status", result.status),
            ("branch", result.branch),
            ("revision", result.revision),
            ("element changes", result.change_count),
            ("data changes", result.data_changes),
        ]
    )
    if result.changes:
        app.out.table([c.model_dump() for c in result.changes], CHANGE_COLUMNS)
