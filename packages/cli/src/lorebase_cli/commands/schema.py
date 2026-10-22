"""`schema get|validate|apply|diff|export|import`: the schema of a Domain as a YAML document."""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from lorebase_cli.context import AppContext, app_of
from lorebase_cli.failures import EXIT_INVALID, CliError
from lorebase_cli.linkml import (
    Conversion,
    LinkmlError,
    LinkmlOptions,
    dump_yaml,
    from_linkml,
    load_yaml,
    merge_into_current,
    to_linkml,
)
from lorebase_cli.options import AtOpt, BranchOpt, DomainOpt, JsonOpt
from lorebase_cli.output import Column
from lorebase_client import NotFoundError
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


class InterchangeFormat(StrEnum):
    """Schema languages `schema export` and `schema import` translate to and from."""

    LINKML = "linkml"


OutputOpt = Annotated[
    Path | None, typer.Option("--output", "-o", help="Write the document to this file.")
]


def _write(app: AppContext, text: str, output: Path | None) -> None:
    if output:
        output.write_text(text, encoding="utf-8")
        app.out.info(f"wrote {output}")
    else:
        app.out.text(text)


@schema_app.command("export")
def schema_export(
    ctx: typer.Context,
    format_: Annotated[
        InterchangeFormat, typer.Option("--format", help="Schema language to write.")
    ],
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    at: AtOpt = None,
    file: Annotated[
        str | None,
        typer.Option(
            "--file",
            help="Convert this Lorebase schema document (`-` reads standard input) instead of "
            "the schema of the Domain.",
        ),
    ] = None,
    uri: Annotated[
        str | None,
        typer.Option("--uri", help="Id of the LinkML schema; `{domain}` is the Domain id."),
    ] = None,
    output: OutputOpt = None,
) -> None:
    """Translate a schema into LinkML YAML.

    What LinkML cannot express (element ids, sensitivity, search settings, Operations,
    Derivations, Sources) travels as `lorebase.*` annotations.
    """
    app = app_of(ctx, False)
    if file is not None:
        text = read_document(file)
    else:
        name = app.domain(domain)
        text = app.client().schema.get_yaml(name, branch=app.branch(branch), at=at)
    options = LinkmlOptions(schema_uri=uri) if uri else LinkmlOptions()
    try:
        schema = to_linkml(load_yaml(text), options)
    except LinkmlError as error:
        raise CliError(str(error), exit_code=EXIT_INVALID) from error
    _write(app, dump_yaml(schema), output)


@schema_app.command("import")
def schema_import(
    ctx: typer.Context,
    file: Annotated[
        str, typer.Argument(help="LinkML schema; `-` reads standard input.", metavar="FILE")
    ],
    source: Annotated[InterchangeFormat, typer.Option("--from", help="Schema language of FILE.")],
    output: OutputOpt = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply the result to a branch instead of printing it."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan applying the result; write nothing.")
    ] = False,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    message: Annotated[
        str | None, typer.Option("--message", "-m", help="Revision message.")
    ] = None,
    cascade: Annotated[
        bool,
        typer.Option("--cascade", help="Allow removing Kinds or Relations that hold data."),
    ] = False,
    expected_head: Annotated[
        str | None,
        typer.Option("--expected-head", help="Fail unless the branch is at this Revision."),
    ] = None,
    prune: Annotated[
        bool,
        typer.Option(
            "--prune",
            help="With --apply or --dry-run: also remove the Kinds, Attributes, Relations and "
            "Traits of the Domain that the LinkML schema leaves out (otherwise kept, with a "
            "warning).",
        ),
    ] = False,
    json_out: JsonOpt = False,
) -> None:
    """Translate a LinkML schema into a Lorebase schema document.

    Prints the document (or writes it with --output); warnings about what it leaves out go to
    standard error. --apply sends it to a branch like `schema apply`, --dry-run plans that. The
    import is merged into the current schema of the Domain: the LinkML schema owns Kinds,
    Attributes, Relations, Traits, enums and path Derivations; Operations, other Derivations,
    Sources and the fields LinkML cannot carry (`derivation`, `authority`, ...) are kept.
    Elements the LinkML schema leaves out stay, with a warning, unless --prune removes them.
    """
    app = app_of(ctx, json_out)
    if prune and not (apply or dry_run):
        raise CliError("--prune needs --apply or --dry-run: nothing is merged otherwise")
    try:
        conversion = from_linkml(load_yaml(read_document(file)))
    except LinkmlError as error:
        raise CliError(str(error), exit_code=EXIT_INVALID) from error
    if not (apply or dry_run):
        for warning in conversion.warnings:
            app.out.info(f"warning: {warning}")
        _print_conversion(app, conversion, output)
        return
    name = app.domain(domain)
    conversion.document["domain"]["id"] = name
    target = _default_branch(app, name, branch)
    try:
        current = load_yaml(app.client().schema.get_yaml(name, branch=target))
    except NotFoundError:
        current = None  # a new Domain or branch: nothing to keep
    if current:
        try:
            conversion = merge_into_current(current, conversion, prune=prune)
        except LinkmlError as error:
            raise CliError(str(error), exit_code=EXIT_INVALID) from error
    for warning in conversion.warnings:
        app.out.info(f"warning: {warning}")
    result = app.client().schema.apply(
        name,
        target,
        yaml=dump_yaml(conversion.document),
        message=message,
        expected_head=expected_head,
        dry_run=dry_run,
        cascade=cascade,
    )
    app.out.emit(result, lambda: _report_apply(app, result))


def _print_conversion(app: AppContext, conversion: Conversion, output: Path | None) -> None:
    if app.json_mode and not output:
        app.out.data(
            {
                "document": conversion.document,
                "warnings": [vars(warning) for warning in conversion.warnings],
            }
        )
        return
    _write(app, dump_yaml(conversion.document), output)
