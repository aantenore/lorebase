"""Token buckets shared by every Keeper (`keeper.limits.rate.backend: postgres`).

A bucket is one row of the UNLOGGED `rate_limit_buckets`, keyed by the SHA-256 of its name.
`lease` refills a bucket to the database clock and takes up to `lease` whole tokens in one
statement (an upsert, so the row lock makes it atomic across Keepers); a Keeper then serves
requests from the tokens it leased without another round trip. Tokens leave the shared bucket
only through leases, so all Keepers together never exceed the limit; leased tokens a Keeper
does not use in time are lost, which only makes the limit stricter. Time is the database's
(`clock_timestamp()`), so the clocks of the Keepers do not matter.

Rows of buckets that are full again are redundant (a missing bucket is a full one): `prune`
deletes them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = "extract(epoch FROM clock_timestamp())"
_REFILLED = (
    "LEAST(CAST(:capacity AS float8), b.tokens + GREATEST(0, EXCLUDED.updated - b.updated) "
    "* CAST(:refill AS float8))"
)
_GRANT = (
    f"CASE WHEN {_REFILLED} >= CAST(:cost AS float8) "
    f"THEN GREATEST(CAST(:cost AS float8), LEAST(CAST(:lease AS float8), floor({_REFILLED}))) "
    "ELSE 0 END"
)
_FULL_AT = "{updated} + (CAST(:capacity AS float8) - {tokens}) / CAST(:refill AS float8)"

_LEASE = text(
    f"""
    INSERT INTO rate_limit_buckets AS b (key, tokens, updated, full_at, granted)
    SELECT :key, CAST(:capacity AS float8) - g.taken, g.now,
           g.now + g.taken / CAST(:refill AS float8), g.taken
    FROM (SELECT {_NOW} AS now,
                 LEAST(CAST(:lease AS float8), floor(CAST(:capacity AS float8))) AS taken) AS g
    ON CONFLICT (key) DO UPDATE SET
        tokens = {_REFILLED} - {_GRANT},
        updated = GREATEST(b.updated, EXCLUDED.updated),
        full_at = {
        _FULL_AT.format(
            updated="GREATEST(b.updated, EXCLUDED.updated)", tokens=f"({_REFILLED} - {_GRANT})"
        )
    },
        granted = {_GRANT}
    RETURNING tokens, granted, updated
    """
)

_PEEK = text(f"SELECT tokens, updated, {_NOW} FROM rate_limit_buckets WHERE key = :key")
_CLOCK = text(f"SELECT {_NOW}")
_PRUNE = text(
    f"""
    DELETE FROM rate_limit_buckets WHERE key IN (
        SELECT key FROM rate_limit_buckets WHERE full_at < {_NOW} LIMIT :limit
    )
    """
)


def bucket_key(name: str) -> bytes:
    """Stored key of a bucket name."""
    return hashlib.sha256(name.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class Lease:
    """Tokens taken from a shared bucket: `granted` (0 when it held fewer than the cost), the
    tokens left in it after the lease and the database time, in epoch seconds."""

    granted: float
    remaining: float
    now: float


@dataclass(frozen=True, slots=True)
class BucketReading:
    """Tokens of a bucket at `updated`, and the database time `now` (epoch seconds). A bucket
    that has no row is full: `tokens` is None."""

    tokens: float | None
    updated: float
    now: float


async def lease(
    session: AsyncSession,
    name: str,
    *,
    capacity: int,
    refill_per_s: float,
    lease: int,
    cost: float = 1.0,
) -> Lease:
    """Refill the bucket and take up to `lease` whole tokens, at least `cost`, or none when it
    holds fewer than `cost`. A bucket without a row starts full."""
    row = (
        await session.execute(
            _LEASE,
            {
                "key": bucket_key(name),
                "capacity": capacity,
                "refill": refill_per_s,
                "lease": max(lease, cost),
                "cost": cost,
            },
        )
    ).one()
    return Lease(granted=float(row[1]), remaining=float(row[0]), now=float(row[2]))


async def read(session: AsyncSession, name: str) -> BucketReading:
    """The stored state of a bucket, without changing it."""
    row = (await session.execute(_PEEK, {"key": bucket_key(name)})).one_or_none()
    if row is None:
        now = float((await session.execute(_CLOCK)).scalar_one())
        return BucketReading(None, now, now)
    return BucketReading(float(row[0]), float(row[1]), float(row[2]))


async def prune(session: AsyncSession, *, limit: int) -> int:
    """Delete up to `limit` rows of buckets that are full again; the number deleted."""
    result = await session.execute(_PRUNE, {"limit": limit})
    return int(getattr(result, "rowcount", 0) or 0)
