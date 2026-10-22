"""Durable memory of the structured output mode each chat endpoint needs.

`lorebase_inference.chat.ModeMemory` over `inference_endpoint_modes`: the mode only moves
towards the weaker ones, so concurrent writers converge on the weakest mode any of them saw,
and an entry older than the caller's maximum age counts as absent (the next write replaces it
and drops the other expired ones).
`clear_modes` forgets every entry (an endpoint that got stronger).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_RECALL = text(
    "SELECT mode_index FROM inference_endpoint_modes "
    "WHERE key = :key AND updated_at > now() - make_interval(secs => :age)"
)
_REMEMBER = text(
    """
    INSERT INTO inference_endpoint_modes AS m (key, mode_index) VALUES (:key, :index)
    ON CONFLICT (key) DO UPDATE
        SET mode_index = CASE
                WHEN m.updated_at > now() - make_interval(secs => :age)
                THEN GREATEST(m.mode_index, EXCLUDED.mode_index)
                ELSE EXCLUDED.mode_index
            END,
            updated_at = now()
    """
)
_PRUNE = text(
    "DELETE FROM inference_endpoint_modes WHERE updated_at <= now() - make_interval(secs => :age)"
)
_CLEAR = text("DELETE FROM inference_endpoint_modes")


class StoreModeMemory:
    """One transaction per call."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def recall(self, digest: bytes, max_age_s: float) -> int | None:
        async with self._sessions() as session:
            found: int | None = (
                await session.execute(_RECALL, {"key": digest, "age": max_age_s})
            ).scalar_one_or_none()
            return found

    async def remember(self, digest: bytes, index: int, max_age_s: float) -> None:
        async with self._sessions.begin() as session:
            await session.execute(_REMEMBER, {"key": digest, "index": index, "age": max_age_s})
            await session.execute(_PRUNE, {"age": max_age_s})


async def clear_modes(sessions: async_sessionmaker[AsyncSession]) -> int:
    """Forget every remembered mode; returns how many entries were removed."""
    async with sessions.begin() as session:
        return (await session.execute(_CLEAR)).rowcount  # type: ignore[attr-defined,no-any-return]
