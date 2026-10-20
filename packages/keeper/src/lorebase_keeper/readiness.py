"""Readiness checks behind `/readyz`.

A check is an async callable with a `name`. The database check connects, runs `SELECT 1` and
compares the Alembic heads the code expects (`packaged_heads`, from the store) with the revision
stored in the `alembic_version` table, so a service whose migrations are missing or behind is not
ready. Check failures are logged in full and reported to callers as a short
reason that never contains connection strings or credentials.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import logging
import re
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from lorebase_platform.secrets import SecretResolutionError, resolve_secret
from lorebase_platform.settings import ConfigError, Settings
from lorebase_store.migrations import head_revisions

logger = logging.getLogger("lorebase.keeper.readiness")

MigrationHeads = Callable[[], Collection[str]]
"""Returns the Alembic head revisions the running code expects; empty skips the comparison."""

_DRIVER_SUFFIX = re.compile(r"^(postgres(?:ql)?)\+\w+://")


@functools.cache
def packaged_heads() -> tuple[str, ...]:
    """Alembic head revisions of the installed store code (read once: they cannot change)."""
    return head_revisions()


class CheckStatus(StrEnum):
    """Outcome of one readiness check."""

    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


@dataclass(frozen=True)
class CheckResult:
    """Status of a check and an optional short reason."""

    status: CheckStatus
    detail: str | None = None

    @property
    def passed(self) -> bool:
        """Failed checks make the service not ready; unconfigured ones do not."""
        return self.status is not CheckStatus.FAILED


class ReadinessCheck(Protocol):
    """One probe of a dependency."""

    name: str

    async def __call__(self) -> CheckResult:
        """Run the probe."""
        ...


class NotConfiguredCheck:
    """Placeholder reporting that a dependency is not part of this deployment."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def __call__(self) -> CheckResult:
        return CheckResult(CheckStatus.NOT_CONFIGURED)


def asyncpg_dsn(dsn: str) -> str:
    """Connection string without a SQLAlchemy driver suffix (`postgresql+asyncpg://` etc.)."""
    return _DRIVER_SUFFIX.sub(r"\1://", dsn)


def _asyncpg_connect(dsn: str, timeout: float) -> Awaitable[Any]:
    asyncpg = importlib.import_module("asyncpg")
    return asyncpg.connect(dsn, timeout=timeout)  # type: ignore[no-any-return]


class DatabaseCheck:
    """Postgres is reachable and its migrations match the code."""

    name = "database"

    def __init__(
        self,
        dsn: str,
        *,
        timeout_s: float,
        expected_heads: MigrationHeads | None = None,
        connect: Callable[[str, float], Awaitable[Any]] = _asyncpg_connect,
    ) -> None:
        self._dsn = asyncpg_dsn(dsn)
        self._timeout_s = timeout_s
        self._expected_heads = expected_heads
        self._connect = connect

    @property
    def expected_heads(self) -> frozenset[str]:
        """Revisions the database must be at; empty when migrations are not verified."""
        return frozenset(self._expected_heads()) if self._expected_heads else frozenset()

    async def __call__(self) -> CheckResult:
        connection = await self._connect(self._dsn, self._timeout_s)
        try:
            await connection.fetchval("SELECT 1")
            expected = self.expected_heads
            if not expected:
                return CheckResult(CheckStatus.OK, "migration revision not verified")
            if await connection.fetchval("SELECT to_regclass('alembic_version')") is None:
                return CheckResult(CheckStatus.FAILED, "migrations have not been applied")
            rows = await connection.fetch("SELECT version_num FROM alembic_version")
        finally:
            await connection.close()
        current = frozenset(row["version_num"] for row in rows)
        if current != expected:
            return CheckResult(
                CheckStatus.FAILED,
                f"migrations at {_join(current)}, expected {_join(expected)}",
            )
        return CheckResult(CheckStatus.OK)


def _join(revisions: Collection[str]) -> str:
    return ", ".join(sorted(revisions)) or "none"


async def run_checks(checks: Sequence[ReadinessCheck], timeout_s: float) -> dict[str, CheckResult]:
    """Run every check concurrently, each within `timeout_s`."""

    async def run(check: ReadinessCheck) -> CheckResult:
        try:
            return await asyncio.wait_for(check(), timeout_s)
        except TimeoutError:
            return CheckResult(CheckStatus.FAILED, f"no answer within {timeout_s:g}s")
        except Exception as exc:
            logger.warning("readiness check %s failed", check.name, exc_info=True)
            return CheckResult(CheckStatus.FAILED, type(exc).__name__)

    results = await asyncio.gather(*(run(check) for check in checks))
    return {check.name: result for check, result in zip(checks, results, strict=True)}


def default_checks(
    settings: Settings,
    *,
    section: str,
    dsn_key: str,
    timeout_s: float,
    expected_heads: MigrationHeads | None,
) -> list[ReadinessCheck]:
    """The database check when the settings hold a connection string, else a placeholder."""
    raw: Mapping[str, Any] = (settings.model_extra or {}).get(section) or {}
    dsn = raw.get(dsn_key)
    if not dsn:
        return [NotConfiguredCheck("database")]
    try:
        resolved = resolve_secret(str(dsn))
    except SecretResolutionError as exc:
        raise ConfigError([f"{section}.{dsn_key}: {exc}"]) from None
    return [DatabaseCheck(resolved, timeout_s=timeout_s, expected_heads=expected_heads)]
