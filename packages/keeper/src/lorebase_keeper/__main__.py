"""Run Keeper, or prepare an installation, with the settings Keeper reads.

python -m lorebase_keeper [serve]
    Serve the API; host and port come from the `keeper` settings.
python -m lorebase_keeper bootstrap [--principal ID] [--name NAME] [--force]
    Create the first installation administrator key and print it once (see bootstrap.py).
python -m lorebase_keeper principal create ID [--name NAME]
    Add a service principal with a chosen id; nothing happens when it exists.
python -m lorebase_keeper audit verify (DOMAIN | --installation | --all) [--after-seq N] [--json]
    Recompute the hash chains of the audit log and report the first broken entry; the exit
    status is 1 when an entry does not verify.

The installation commands connect with `database.maintenance_dsn` when it is set, else with
`database.dsn`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from datetime import UTC, datetime
from typing import Any

import uvicorn
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_keeper.app import create_app
from lorebase_keeper.bootstrap import (
    DEFAULT_ADMIN_NAME,
    BootstrapError,
    bootstrap,
    create_service_principal,
)
from lorebase_keeper.settings import keeper_settings
from lorebase_platform.logging import configure_logging
from lorebase_platform.settings import ConfigError, Settings, load_settings
from lorebase_platform.telemetry import setup_telemetry
from lorebase_store.auth import Chains, ChainVerification, SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.settings import DatabaseSettings

SERVICE_NAME = "keeper"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lorebase_keeper",
        description="Run Keeper or prepare an installation with Keeper's settings.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="serve the API (the default)")
    boot = commands.add_parser(
        "bootstrap",
        help="create the first installation administrator key and print it once",
        description="Create a key for an installation administrator (keeper.auth."
        "installation_admins) and print it on standard output, once. Refused when an "
        "installation administrator already holds a usable key, unless --force is given.",
    )
    boot.add_argument(
        "--principal",
        help="administrator principal id (default: the first of keeper.auth.installation_admins)",
    )
    boot.add_argument(
        "--name",
        default=DEFAULT_ADMIN_NAME,
        help="display name when the principal is created",
    )
    boot.add_argument(
        "--force",
        action="store_true",
        help="add a key even when an administrator already holds one (existing keys stay valid)",
    )
    principal = commands.add_parser("principal", help="manage service principals")
    principal_commands = principal.add_subparsers(dest="principal_command", required=True)
    create = principal_commands.add_parser(
        "create",
        help="add a service principal with a chosen id",
        description="Add a service principal with a chosen id, for example one that a schema "
        "names in Operation permissions. Nothing happens when it already exists.",
    )
    create.add_argument("principal_id", metavar="ID")
    create.add_argument("--name", help="display name (default: the id)")
    audit = commands.add_parser("audit", help="check the audit log")
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    verify = audit_commands.add_parser(
        "verify",
        help="recompute the audit hash chains and report the first broken entry",
        description="Recompute the hash chain of a Domain's audit log, of the installation-wide "
        "chain or of every chain, reading entries in batches. Exits with status 1 when an entry "
        "does not verify.",
    )
    chain = verify.add_mutually_exclusive_group(required=True)
    chain.add_argument("domain", nargs="?", metavar="DOMAIN", help="Domain id")
    chain.add_argument(
        "--installation", action="store_true", help="the chain of entries without a Domain"
    )
    chain.add_argument("--all", action="store_true", help="every chain")
    verify.add_argument(
        "--after-seq",
        type=int,
        default=0,
        metavar="N",
        help="start after entry N, trusting it (the last_seq of an earlier run); default 0",
    )
    verify.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch to serving (no arguments) or to an installation command."""
    args = _parser().parse_args(argv)
    if args.command == "bootstrap":
        _run_admin(lambda settings: _bootstrap(settings, args))
    elif args.command == "principal":
        _run_admin(lambda settings: _create_principal(settings, args))
    elif args.command == "audit":
        _run_admin(lambda settings: _verify_audit(settings, args))
    else:
        serve()


def serve() -> None:
    """Load settings, configure logging and telemetry, and serve until stopped."""
    try:
        settings = load_settings()
        keeper = keeper_settings(settings)
        configure_logging(settings.logging, service=SERVICE_NAME)
        telemetry = setup_telemetry(settings.telemetry)
        app = create_app(settings, telemetry=telemetry)
    except ConfigError as exc:
        sys.exit(str(exc))
    try:
        uvicorn.run(
            app,
            host=keeper.host,
            port=keeper.port,
            log_config=None,
            access_log=False,
            server_header=False,
            # The client address comes from `keeper.trusted_proxies` (client_address.py) only.
            proxy_headers=False,
        )
    finally:
        telemetry.shutdown()


def _run_admin(command: Callable[[Settings], Coroutine[Any, Any, None]]) -> None:
    """Load settings and run an installation command; problems exit with a message."""
    try:
        settings = load_settings()
        configure_logging(settings.logging, service=SERVICE_NAME)
        asyncio.run(command(settings))
    except (ConfigError, BootstrapError) as exc:
        sys.exit(f"lorebase_keeper: {exc}")


def _store(settings: Settings) -> tuple[SqlAuthStore, Callable[[], Awaitable[None]]]:
    """Auth store of the installation commands: the owner role of `database.maintenance_dsn`
    when it is set (the services may connect as a role subject to row-level security), else
    `database.dsn`."""
    database = settings.section("database", DatabaseSettings)
    owner = database.maintenance_dsn
    engine = create_engine(
        database, url=None if owner is None else owner.get_secret_value(), pooled=False
    )
    return SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False)), engine.dispose


async def _bootstrap(settings: Settings, args: argparse.Namespace) -> None:
    auth = keeper_settings(settings).auth
    store, dispose = _store(settings)
    try:
        created = await bootstrap(
            store,
            auth,
            now=datetime.now(UTC),
            principal_id=args.principal,
            display_name=args.name,
            force=args.force,
        )
    finally:
        await dispose()
    expiry = created.expires_at.date().isoformat() if created.expires_at else "never"
    action = "created" if created.principal_created else "existing"
    print(
        f"API key {created.prefix}... for {action} principal {created.principal_id} "
        f"(expires {expiry}); it is printed once on standard output",
        file=sys.stderr,
    )
    print(created.token)


async def _create_principal(settings: Settings, args: argparse.Namespace) -> None:
    store, dispose = _store(settings)
    try:
        added = await create_service_principal(
            store, args.principal_id, args.name or args.principal_id, now=datetime.now(UTC)
        )
    finally:
        await dispose()
    state = "created" if added else "already exists"
    print(f"service principal {args.principal_id} {state}", file=sys.stderr)


def _chain_of(args: argparse.Namespace) -> str | Chains | None:
    if args.all:
        return Chains.ALL
    return None if args.installation else str(args.domain)


def verification_report(chain: str | Chains | None, result: ChainVerification) -> dict[str, Any]:
    """JSON form of a verification, as `audit verify --json` prints it."""
    broken = (
        None
        if result.ok
        else {"seq": result.bad_seq, "domain_id": result.bad_domain_id, "reason": result.reason}
    )
    return {
        "chain": chain.value if isinstance(chain, Chains) else chain,
        "intact": result.ok,
        "checked": result.checked,
        "chains": result.chains,
        "last_seq": result.last_seq,
        "first_broken": broken,
    }


async def _verify_audit(settings: Settings, args: argparse.Namespace) -> None:
    if args.after_seq < 0:
        raise BootstrapError("--after-seq must not be negative")
    chain = _chain_of(args)
    store, dispose = _store(settings)
    try:
        async with store.unit() as unit:
            result = await unit.audit.verify(after_seq=args.after_seq, domain_id=chain)
    finally:
        await dispose()
    if args.json:
        print(json.dumps(verification_report(chain, result)))
    elif result.ok:
        last = "-" if result.last_seq is None else result.last_seq
        print(
            f"audit log intact: {result.checked} entries in {result.chains} chain(s), "
            f"last seq {last}"
        )
    else:
        where = result.bad_domain_id or "installation"
        print(
            f"audit log broken at seq {result.bad_seq} (chain {where}): {result.reason}; "
            f"{result.checked} entries verified before it"
        )
    if not result.ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
