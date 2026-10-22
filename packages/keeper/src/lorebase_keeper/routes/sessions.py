"""Browser sign-in for the web app: Keeper as backend-for-frontend (design 13.2).

`GET /auth/login` sends the browser to the provider, `GET /auth/callback` turns the answer into
a session cookie, `GET /auth/session` gives the app what it needs while signed in (who, until
when, the CSRF token) and `POST /auth/logout` ends the session. The logic lives in
`lorebase_keeper.auth.sessions`; every route answers 404 while `keeper.auth.sessions.enabled`
is false.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.config import SessionSettings, is_local_path, url_origin
from lorebase_keeper.auth.deps import AuthDep, PrincipalDep
from lorebase_keeper.auth.oidc import ProviderUnavailableError
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.auth.sessions import (
    CALLBACK_PATH,
    LOGIN_PATH,
    LOGOUT_PATH,
    LoginError,
    LoginErrorCode,
    SessionManager,
)
from lorebase_keeper.errors import (
    BAD_REQUEST,
    FORBIDDEN,
    NOT_FOUND,
    UNAUTHORIZED,
    UNAVAILABLE,
    Problem,
    ProblemError,
)
from lorebase_store.auth import NewAuditEntry

logger = logging.getLogger("lorebase.keeper.auth.sessions")

router = APIRouter(tags=["auth"])

_NO_STORE = {"Cache-Control": "no-store"}
_CALLBACK_PARAMS = ("code", "state", "error")
_DISABLED: dict[int | str, dict[str, Any]] = {
    404: {"model": Problem, "description": "Web sign-in is not enabled."}
}


class SessionInfo(BaseModel):
    """The browser session of the caller."""

    principal_id: str
    display_name: str
    csrf_header: str = Field(description="Header to send the CSRF token in on unsafe requests.")
    csrf_token: str = Field(description="CSRF token of this session.")
    expires_at: datetime = Field(description="End of the session's absolute lifetime.")
    idle_timeout_s: int = Field(description="The session ends after this long without use.")


class LogoutResult(BaseModel):
    """Where to send the browser to sign out at the identity provider too."""

    logout_url: str | None = Field(
        description="The provider's end-session URL; null when it offers none or no session "
        "was open."
    )


def _manager(auth: AuthRuntime) -> SessionManager:
    if auth.sessions is None:
        raise ProblemError(
            NOT_FOUND, "Web sign-in is not enabled (keeper.auth.sessions.enabled is false)."
        )
    return auth.sessions


def _set_cookie(
    response: Response,
    settings: SessionSettings,
    name: str,
    value: str,
    *,
    max_age: int,
    same_site: Literal["lax", "strict"],
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite=same_site,
    )


def _clear_cookie(response: Response, settings: SessionSettings, name: str) -> None:
    response.delete_cookie(
        name, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax"
    )


def _app_location(settings: SessionSettings, path: str) -> str:
    return url_origin(settings.app_url or "") + path


def _error_redirect(settings: SessionSettings, code: LoginErrorCode) -> RedirectResponse:
    separator = "&" if "?" in settings.error_path else "?"
    location = _app_location(settings, settings.error_path) + separator + urlencode({"error": code})
    response = RedirectResponse(location, status_code=302, headers=_NO_STORE)
    _clear_cookie(response, settings, settings.secure_login_cookie_name)
    return response


@router.get(
    LOGIN_PATH,
    summary="Start a browser sign-in",
    status_code=302,
    response_class=RedirectResponse,
    responses={
        302: {"description": "Redirect to the identity provider."},
        400: {"model": Problem, "description": "return_to is not a path of the web app."},
        **_DISABLED,
        503: {"model": Problem, "description": "The identity provider is unavailable."},
    },
)
async def login(
    auth: AuthDep,
    return_to: Annotated[
        str | None,
        Query(
            max_length=2048,
            description="Path on the web app's origin to return to after signing in.",
        ),
    ] = None,
) -> RedirectResponse:
    """Send the browser to the identity provider (authorization code flow with PKCE). The
    sign-in state travels in a short-lived, encrypted, HttpOnly cookie."""
    sessions = _manager(auth)
    settings = sessions.settings
    path = return_to or urlsplit(settings.app_url or "").path or "/"
    if not is_local_path(path):
        raise ProblemError(BAD_REQUEST, "return_to must be a path on the web app's origin.")
    try:
        start = await sessions.begin_login(path)
    except ProviderUnavailableError:
        raise ProblemError(UNAVAILABLE, "The identity provider is unavailable.") from None
    response = RedirectResponse(start.url, status_code=302, headers=_NO_STORE)
    # Lax whatever `same_site` says: the provider's redirect back is a cross-site navigation.
    _set_cookie(
        response,
        settings,
        settings.secure_login_cookie_name,
        start.state_cookie,
        max_age=settings.login_timeout_s,
        same_site="lax",
    )
    return response


@router.get(
    CALLBACK_PATH,
    summary="Finish a browser sign-in",
    status_code=302,
    response_class=RedirectResponse,
    responses={
        302: {
            "description": "Signed in: redirect to the return path with the session cookie. "
            "Failed: redirect to the app's error path with ?error=<code>."
        },
        **_DISABLED,
    },
)
async def callback(request: Request, auth: AuthDep) -> RedirectResponse:
    """The provider's redirect after sign-in. Checks state, redeems the code with the PKCE
    verifier, verifies the ID token and its nonce, maps the user to a principal as for bearer
    tokens, and starts a session with a new id."""
    sessions = _manager(auth)
    settings = sessions.settings
    query = request.query_params
    try:
        if any(len(query.getlist(name)) > 1 for name in _CALLBACK_PARAMS):
            raise LoginError("login_failed", "repeated callback parameter")
        params = {name: query[name] for name in _CALLBACK_PARAMS if name in query}
        result = await sessions.finish_login(
            params, request.cookies.get(settings.secure_login_cookie_name)
        )
        try:
            principal = await auth.authenticator.user_principal(result.claims)
        except ProblemError as exc:
            code: LoginErrorCode = "no_account" if exc.problem is FORBIDDEN else "login_failed"
            raise LoginError(code, exc.problem_detail or exc.problem.code) from None
    except LoginError as exc:
        logger.info("sign-in failed (%s): %s", exc.code, exc)
        return _error_redirect(settings, exc.code)
    created = await sessions.create(
        principal.id, result, replaces=request.cookies.get(settings.secure_cookie_name)
    )
    logger.info("session started", extra={"principal": principal.id})
    annotate(request, actor=principal.id)
    if auth.audit_writer is not None:
        await auth.audit_writer.write(
            NewAuditEntry(
                actor=principal.id,
                action="auth.session.create",
                details={"outcome": "success", "method": "GET", "route": CALLBACK_PATH},
            )
        )
    response = RedirectResponse(
        _app_location(settings, result.return_to), status_code=302, headers=_NO_STORE
    )
    _set_cookie(
        response,
        settings,
        settings.secure_cookie_name,
        created.session_id,
        max_age=settings.absolute_timeout_s,
        same_site=settings.same_site,
    )
    _clear_cookie(response, settings, settings.secure_login_cookie_name)
    return response


@router.get(
    "/auth/session",
    summary="Describe the browser session",
    responses={
        401: {"model": Problem, "description": "No valid session cookie."},
        **_DISABLED,
    },
)
async def session_info(
    request: Request, response: Response, principal: PrincipalDep, auth: AuthDep
) -> SessionInfo:
    """Who is signed in, until when, and the CSRF token the web app sends with unsafe
    requests. Requests authenticated by an API key or bearer token get 401."""
    sessions = _manager(auth)
    session_id = auth.authenticator.session_id(request.headers)
    if principal.method != "session" or session_id is None or not principal.session_expires_at:
        raise ProblemError(UNAUTHORIZED, "No browser session: sign in through /auth/login.")
    response.headers.update(_NO_STORE)
    return SessionInfo(
        principal_id=principal.id,
        display_name=principal.display_name,
        csrf_header=sessions.settings.csrf_header,
        csrf_token=sessions.csrf_token(session_id),
        expires_at=principal.session_expires_at,
        idle_timeout_s=sessions.settings.idle_timeout_s,
    )


@router.post(
    LOGOUT_PATH,
    summary="Sign out of the browser session",
    response_model=LogoutResult,
    responses={
        403: {"model": Problem, "description": "CSRF check failed."},
        **_DISABLED,
    },
)
async def logout(request: Request, auth: AuthDep) -> Response:
    """End the session of the cookie (CSRF token required), clear the cookie and return the
    provider's end-session URL for RP-initiated logout. Without a session it only clears the
    cookie."""
    sessions = _manager(auth)
    settings = sessions.settings
    session_id = auth.authenticator.session_id(request.headers)
    logout_url: str | None = None
    if session_id is not None:
        sessions.check_request(session_id, request.method, request.headers)
        ended = await sessions.end(session_id)
        if ended.principal_id is not None:
            annotate(request, actor=ended.principal_id, action="auth.session.delete")
            logger.info("session ended by sign-out", extra={"principal": ended.principal_id})
        logout_url = ended.logout_url
    body = LogoutResult(logout_url=logout_url).model_dump_json()
    response = Response(body, media_type="application/json", headers=_NO_STORE)
    _clear_cookie(response, settings, settings.secure_cookie_name)
    return response
