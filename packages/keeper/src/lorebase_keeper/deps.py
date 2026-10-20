"""FastAPI dependencies shared by the routes."""

from __future__ import annotations

import re
from typing import Annotated, cast

from fastapi import Depends, Header, Request

from lorebase_keeper.errors import VALIDATION_FAILED, ProblemError, request_id_of
from lorebase_keeper.settings import KeeperSettings
from lorebase_platform.settings import Settings

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")


def get_settings(request: Request) -> Settings:
    """Process settings the application was built with."""
    return cast(Settings, request.app.state.settings)


def get_keeper_settings(request: Request) -> KeeperSettings:
    """The validated `keeper` section."""
    return cast(KeeperSettings, request.app.state.keeper_settings)


def get_request_id(request: Request) -> str | None:
    """Id assigned to the current request by the request context middleware."""
    return request_id_of(request.scope.get("state"))


def get_idempotency_key(
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    """Client-chosen key that makes a write safe to retry; `None` when the header is absent."""
    if key is not None and not _IDEMPOTENCY_KEY.match(key):
        raise ProblemError(
            VALIDATION_FAILED,
            "Idempotency-Key must be 1 to 255 characters of letters, digits and . _ : -",
        )
    return key


SettingsDep = Annotated[Settings, Depends(get_settings)]
KeeperSettingsDep = Annotated[KeeperSettings, Depends(get_keeper_settings)]
RequestIdDep = Annotated[str | None, Depends(get_request_id)]
IdempotencyKeyDep = Annotated[str | None, Depends(get_idempotency_key)]
