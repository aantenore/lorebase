"""`apikey create|list|revoke`: API keys of a Domain; `admin erase`: erasure of personal data;
`admin vault status|rotate`: vault key rotation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import JsonValue

from lorebase_cli.context import app_of
from lorebase_cli.failures import CliError
from lorebase_cli.options import AllOpt, DomainOpt, JsonOpt, LimitOpt
from lorebase_cli.output import Column

apikey_app = typer.Typer(help="Manage the API keys of a Domain.", no_args_is_help=True)
admin_app = typer.Typer(help="Administer the personal data of a Domain.", no_args_is_help=True)
vault_app = typer.Typer(
    help="Rotate the vault keys (installation administrators).", no_args_is_help=True
)
admin_app.add_typer(vault_app, name="vault")

KEY_COLUMNS = [
    Column("id", "id"),
    Column("prefix", "prefix"),
    Column("principal", "principal_id"),
    Column("expires", "expires_at"),
    Column("last used", "last_used_at"),
    Column("revoked", "revoked_at"),
    Column("networks", "allowed_networks"),
]


@apikey_app.command("create")
def apikey_create(
    ctx: typer.Context,
    name: Annotated[
        str,
        typer.Argument(help="Name of the service principal the key belongs to.", metavar="NAME"),
    ],
    domain: DomainOpt = None,
    role: Annotated[
        str | None,
        typer.Option("--role", help="Role the principal gets in the Domain (server default)."),
    ] = None,
    ttl_days: Annotated[
        int | None, typer.Option("--ttl-days", min=1, help="Lifetime in days (server default).")
    ] = None,
    principal_id: Annotated[
        str | None,
        typer.Option("--principal-id", help="Add a key to an existing service principal."),
    ] = None,
    allow_network: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-network",
            metavar="CIDR",
            help="Client network the key may be used from, e.g. 203.0.113.0/24; repeatable. "
            "Without it the key works from any address.",
        ),
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """Create an API key. The secret is shown once; store it right away."""
    app = app_of(ctx, json_out)
    created = app.client().api_keys.create(
        app.domain(domain),
        name,
        role=role,
        ttl_days=ttl_days,
        principal_id=principal_id,
        allowed_networks=allow_network,
    )

    def human() -> None:
        app.out.pairs(
            [
                ("key", created.key),
                ("id", created.id),
                ("principal", created.principal_id),
                ("role", created.role),
                ("expires", created.expires_at),
                ("networks", ", ".join(created.allowed_networks) or "any"),
            ]
        )
        app.out.info("This is the only time the key is shown: store it now.")

    app.out.emit(created, human)


@apikey_app.command("list")
def apikey_list(
    ctx: typer.Context,
    domain: DomainOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the API keys of a Domain, including revoked and expired ones."""
    app = app_of(ctx, json_out)
    items, more = app.take(app.client().api_keys.iterate(app.domain(domain)), limit, everything)
    app.out.emit(items, lambda: app.out.table([k.model_dump() for k in items], KEY_COLUMNS))
    if more:
        app.note_truncated(len(items))


@apikey_app.command("revoke")
def apikey_revoke(
    ctx: typer.Context,
    key_id: Annotated[
        str, typer.Argument(help="Id of the key (see `apikey list`).", metavar="KEY_ID")
    ],
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Revoke an API key; it stops working at once."""
    app = app_of(ctx, json_out)
    app.client().api_keys.revoke(app.domain(domain), key_id)
    app.out.emit({"revoked": key_id}, lambda: app.out.info(f"revoked key {key_id}"))


@admin_app.command("erase")
def admin_erase(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Kind api name.", metavar="KIND")],
    key: Annotated[
        str | None, typer.Option("--key", help="Erase the values of the Record with this key.")
    ] = None,
    attribute: Annotated[
        list[str] | None,
        typer.Option(
            "--attribute",
            help="Vault Attribute (api name). With --key: limit to it (repeatable); with "
            "--value: the Attribute whose value is erased in every Record.",
        ),
    ] = None,
    value: Annotated[
        str | None,
        typer.Option(
            "--value",
            metavar="- | @FILE",
            help="Where the value to erase is read from (JSON, or plain text when it is not "
            "valid JSON): - for standard input (asked without echo on a terminal), @FILE for a "
            "file. A value given on the command line is refused: it would stay in the shell "
            "history and the process list.",
        ),
    ] = None,
    branch: Annotated[
        str | None, typer.Option("--branch", help="Branch whose schema names the Kind.")
    ] = None,
    domain: DomainOpt = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    json_out: JsonOpt = False,
) -> None:
    """Erase personal data from the vault, on every branch and Revision. This cannot be undone.

    `--key K` erases what the Record ever held in its vault Attributes; `--attribute A --value
    V` erases V of A in every Record of the Kind. History keeps its shape: reads show the
    values as erased.
    """
    app = app_of(ctx, json_out)
    if (key is None) == (value is None):
        raise CliError("give --key, or --attribute and --value")
    attributes = attribute or []
    if value is not None and len(attributes) != 1:
        raise CliError("erasure by --value takes exactly one --attribute")
    text = None if value is None else _value_text(value)
    target = f"{kind}/{key}" if key is not None else f"a value of {kind}.{attributes[0]}"
    if not yes:
        if not sys.stdin.isatty():
            raise CliError("erasure is irreversible: pass --yes to confirm")
        typer.confirm(f"Erase {target} from the vault for good?", abort=True)
    vault = app.client().vault
    if key is not None:
        result = vault.erase_record(
            app.domain(domain), kind, key, attributes=attributes or None, branch=branch
        )
    else:
        result = vault.erase_value(
            app.domain(domain),
            kind,
            attributes[0],
            _parsed(text or ""),
            branch=branch,
        )
    app.out.emit(result, lambda: app.out.info(f"erased {result.erased} vault value(s)"))


def _value_text(source: str) -> str:
    """The value read from standard input (`-`) or a file (`@FILE`)."""
    if source == "-":
        if sys.stdin.isatty():
            return str(typer.prompt("Value to erase", hide_input=True))
        return sys.stdin.read().rstrip("\n")
    if source.startswith("@") and len(source) > 1:
        try:
            return Path(source[1:]).read_text(encoding="utf-8").rstrip("\n")
        except OSError as error:
            raise CliError(f"cannot read {source[1:]}: {error.strerror}") from None
    raise CliError(
        "give the value as --value - (standard input) or --value @FILE: a value on the "
        "command line stays in the shell history and the process list"
    )


def _parsed(text: str) -> JsonValue:
    try:
        parsed: JsonValue = json.loads(text)
    except ValueError:
        return text
    return parsed


@vault_app.command("status")
def vault_status(ctx: typer.Context, json_out: JsonOpt = False) -> None:
    """Show which keys the vault rows use and which earlier keys can be removed."""
    app = app_of(ctx, json_out)
    keys = app.client().vault.keys()

    def human() -> None:
        app.out.pairs(
            [
                ("encryption key", keys.key_id),
                ("token key", keys.token_key_id),
                ("rows by encryption key", _counts(keys.encryption_keys)),
                ("rows by fingerprint key", _counts(keys.fingerprint_keys)),
                ("tombstones by fingerprint key", _counts(keys.tombstone_keys)),
                ("rows to rotate", keys.outdated),
                ("removable previous_keys", ", ".join(keys.removable_keys) or "none"),
                (
                    "removable previous_token_keys",
                    ", ".join(keys.removable_token_keys) or "none",
                ),
                ("digest key", keys.digests),
            ]
        )

    app.out.emit(keys, human)


@vault_app.command("rotate")
def vault_rotate(ctx: typer.Context, json_out: JsonOpt = False) -> None:
    """Queue the job that seals and fingerprints the vault rows of every Domain again under the
    current keys, in batches. Follow it with `admin vault status`."""
    app = app_of(ctx, json_out)
    queued = app.client().vault.rotate()
    app.out.emit(queued, lambda: app.out.info(f"queued vault rotation job {queued.job_id}"))


def _counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}: {count}" for key, count in counts.items()) or "none"
