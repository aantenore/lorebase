"""Run Keeper, or prepare an installation, with the settings Keeper reads.

python -m lorebase_keeper [serve]
    Serve the API; host and port come from the `keeper` settings.
python -m lorebase_keeper bootstrap [--principal ID] [--name NAME] [--force]
    Create the first installation administrator key and print it once (see bootstrap.py).
python -m lorebase_keeper principal create ID [--name NAME]
    Add a service principal with a chosen id; nothing happens when it exists.
python -m lorebase_keeper audit verify (DOMAIN | --installation | --all) [--after-seq N] [--json]
        [--reads] [--checkpoints PATH ... --checkpoint-key FILE]
    Recompute the hash chains of the audit log (the read stream with --reads) and report the
    first broken entry; with --checkpoints, also compare the log with the signed checkpoints
    Scribe published (files or directories of them) to detect removed or rewritten entries.
    The exit status is 1 when an entry does not verify, a checkpointed entry is missing or
    changed, or a checkpoint is invalid.

The installation commands connect with `database.maintenance_dsn` when it is set, else with
`database.dsn`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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
from lorebase_store.auth import AuditStream, Chains, ChainVerification, SqlAuthStore
from lorebase_store.auth.checkpoints import (
    Checkpoint,
    CheckpointError,
    CheckpointFinding,
    compare,
    load_public_key,
    parse_document,
)
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
    verify.add_argument(
        "--reads", action="store_true", help="the read stream (audit_read_log) instead of actions"
    )
    verify.add_argument(
        "--checkpoints",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="checkpoint file, or directory searched for *.json (repeatable)",
    )
    verify.add_argument(
        "--checkpoint-key",
        type=Path,
        metavar="FILE",
        help="PEM Ed25519 public key the checkpoints are signed with",
    )
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


def verification_report(
    chain: str | Chains | None,
    result: ChainVerification,
    stream: AuditStream = AuditStream.ACTIONS,
    anchored: Anchoring | None = None,
) -> dict[str, Any]:
    """JSON form of a verification, as `audit verify --json` prints it."""
    broken = (
        None
        if result.ok
        else {"seq": result.bad_seq, "domain_id": result.bad_domain_id, "reason": result.reason}
    )
    report: dict[str, Any] = {
        "stream": stream.value,
        "chain": chain.value if isinstance(chain, Chains) else chain,
        "intact": result.ok and (anchored is None or anchored.ok),
        "checked": result.checked,
        "chains": result.chains,
        "last_seq": result.last_seq,
        "first_broken": broken,
    }
    if anchored is not None:
        report["checkpoints"] = {
            "checked": anchored.checked,
            "invalid": anchored.invalid,
            "findings": [
                {
                    "checkpoint_at": f.checkpoint_at.isoformat(),
                    "domain_id": f.domain_id,
                    "seq": f.seq,
                    "problem": f.problem,
                }
                for f in anchored.findings
            ],
        }
    return report


@dataclass(frozen=True)
class Anchoring:
    """Comparison of the log with checkpoints: how many were compared, the files that are
    not valid checkpoints, and the checkpointed entries the log no longer holds as they were."""

    checked: int
    invalid: list[str]
    findings: list[CheckpointFinding]

    @property
    def ok(self) -> bool:
        return not self.invalid and not self.findings


def _checkpoint_files(paths: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.rglob("*.json")) if path.is_dir() else [path])
    return files


def _load_checkpoints(
    paths: Sequence[Path], key_file: Path, stream: AuditStream
) -> tuple[list[Checkpoint], list[str]]:
    """Valid checkpoints of `stream` and the files that are not valid checkpoints."""
    try:
        key = load_public_key(key_file.read_text(encoding="utf-8"))
    except (OSError, CheckpointError) as error:
        raise BootstrapError(f"--checkpoint-key: {error}") from None
    found: list[Checkpoint] = []
    invalid: list[str] = []
    for file in _checkpoint_files(paths):
        try:
            checkpoint = parse_document(file.read_bytes(), key)
        except (OSError, CheckpointError) as error:
            invalid.append(f"{file}: {error}")
            continue
        if checkpoint.stream is stream:
            found.append(checkpoint)
    return found, invalid


async def _verify_audit(settings: Settings, args: argparse.Namespace) -> None:
    if args.after_seq < 0:
        raise BootstrapError("--after-seq must not be negative")
    if args.checkpoints and args.checkpoint_key is None:
        raise BootstrapError("--checkpoints needs --checkpoint-key")
    chain = _chain_of(args)
    stream = AuditStream.READS if args.reads else AuditStream.ACTIONS
    checkpoints, invalid = (
        _load_checkpoints(args.checkpoints, args.checkpoint_key, stream)
        if args.checkpoints
        else ([], [])
    )
    anchored: Anchoring | None = None
    store, dispose = _store(settings)
    try:
        async with store.unit() as unit:
            repository = unit.read_audit if args.reads else unit.audit
            result = await repository.verify(after_seq=args.after_seq, domain_id=chain)
            if args.checkpoints:
                domains = None if isinstance(chain, Chains) else [chain]
                findings = await compare(repository, checkpoints, domain_ids=domains)
                anchored = Anchoring(len(checkpoints), invalid, findings)
    finally:
        await dispose()
    if args.json:
        print(json.dumps(verification_report(chain, result, stream, anchored)))
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
    if anchored is not None and not args.json:
        print(f"checkpoints compared: {anchored.checked}")
        for problem in anchored.invalid:
            print(f"invalid checkpoint {problem}")
        for finding in anchored.findings:
            where = finding.domain_id or "installation"
            print(
                f"entry seq {finding.seq} (chain {where}) of the checkpoint of "
                f"{finding.checkpoint_at.isoformat()} is {finding.problem}"
            )
    if not result.ok or (anchored is not None and not anchored.ok):
        sys.exit(1)


if __name__ == "__main__":
    main()
