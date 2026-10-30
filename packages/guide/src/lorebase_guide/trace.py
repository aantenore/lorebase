"""Agent trace of an exploration (docs/agent-trace.md).

Each exploration is one trace session: every Keeper read carries the session header, the
opt-in header and the name of the tool that made it, so Keeper records the timeline of the
calls; the model's reasoning between calls is posted as session notes. Notes are best effort:
a Keeper without the notes route (or with tracing off) never fails an exploration, and after
the first refusal no further note of the session is sent.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from lorebase_client import AsyncLorebaseClient, Call
from lorebase_client.errors import LorebaseError
from lorebase_guide.settings import TraceSettings

logger = logging.getLogger("lorebase.guide.trace")

SESSION_PREFIX = "guide-"


def new_session_id() -> str:
    """A session id in the shape Keeper accepts (`[A-Za-z0-9._:-]{1,128}`)."""
    return SESSION_PREFIX + uuid.uuid4().hex


def _ignore(response: httpx.Response) -> None:
    """Notes are fire and forget: the answer is not read."""


class TraceSession:
    """Headers and notes of one exploration's trace session."""

    def __init__(self, settings: TraceSettings, session_id: str, domain: str) -> None:
        self.settings = settings
        self.session_id = session_id
        self.domain = domain
        self._notes_enabled = settings.enabled and settings.notes

    def headers(self) -> dict[str, str]:
        """Headers sent with every Keeper request of the exploration."""
        if not self.settings.enabled:
            return {}
        headers = {self.settings.session_header: self.session_id}
        if self.settings.opt_in_value:
            headers[self.settings.opt_in_header] = self.settings.opt_in_value
        return headers

    def tool_headers(self, tool: str) -> dict[str, str]:
        """Header naming the tool of one Keeper request."""
        return {self.settings.tool_header: f"guide.{tool}"} if self.settings.enabled else {}

    async def note(self, client: AsyncLorebaseClient, text: str) -> bool:
        """Post `text` as a note of the session; False when it was not accepted."""
        text = text.strip()
        if not self._notes_enabled or not text:
            return False
        path = self.settings.notes_path.replace("{session}", self.session_id)
        body = {"text": text[: self.settings.max_note_chars], "domain_id": self.domain}
        try:
            await client.send(Call("POST", path, _ignore, body=body))
        except LorebaseError as exc:
            self._notes_enabled = False
            logger.info(
                "trace notes disabled for this session",
                extra={"status": exc.status, "session": self.session_id},
            )
            return False
        return True
