"""Enforcement of rate limits and quotas on requests.

`enforce_limits` is an application-wide dependency: for every route that is not exempt it
authenticates the caller, takes a token from the caller's bucket for the request's class and,
for API keys, counts the request against the daily quota. Refusals are 429 problem details with
`Retry-After`. `RateLimitHeadersMiddleware` adds the `RateLimit-*` headers of the bucket to
every response of a limited route, errors included. Context-pack tokens are counted by the
route that builds the pack (`check_context_quota`, `charge_context_tokens`).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, cast

from fastapi import Request, Response
from opentelemetry.metrics import MeterProvider
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lorebase_keeper.auth.audit import suppress
from lorebase_keeper.auth.deps import current_principal, get_auth
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.errors import RATE_LIMITED, ProblemError, ProblemType
from lorebase_keeper.ratelimit.bucket import Decision, InMemoryRateStore
from lorebase_keeper.ratelimit.config import QuotaSettings, RateLimitSettings, RouteClass
from lorebase_keeper.ratelimit.limiter import RateLimiter, ThrottleWatch
from lorebase_keeper.ratelimit.quotas import QuotaKind, QuotaSnapshot, QuotaTracker
from lorebase_store.auth import NewAuditEntry

logger = logging.getLogger("lorebase.keeper.limits")

QUOTA_EXCEEDED = ProblemType("quota-exceeded", 429, "Daily quota exceeded")
STATE_KEY = "rate_limit"
THROTTLED_ACTION = "rate_limit.throttled"
LIMIT_HEADER = "RateLimit-Limit"
REMAINING_HEADER = "RateLimit-Remaining"
RESET_HEADER = "RateLimit-Reset"
RETRY_HEADER = "Retry-After"
EXPOSED_HEADERS = (LIMIT_HEADER, REMAINING_HEADER, RESET_HEADER, RETRY_HEADER)


@dataclass
class LimitRuntime:
    """The limiting components of one application."""

    rate: RateLimitSettings
    quota: QuotaSettings
    limiter: RateLimiter | None
    quotas: QuotaTracker | None
    watch: ThrottleWatch | None
    auth: AuthRuntime

    def exempt(self, route_path: str) -> bool:
        """Whether the route (path template without the API prefix) is neither limited nor
        counted."""
        return route_path in self.rate.exempt_paths

    async def aclose(self) -> None:
        """Write the counted use that is still in memory."""
        if self.quotas is not None:
            await self.quotas.aclose()


def build_limits(
    rate: RateLimitSettings,
    quota: QuotaSettings,
    auth: AuthRuntime,
    *,
    meter_provider: MeterProvider | None = None,
) -> LimitRuntime | None:
    """The components for the settings; `None` when neither limit applies."""
    limiter = (
        RateLimiter(rate, InMemoryRateStore(rate.max_buckets), meter_provider=meter_provider)
        if rate.enabled
        else None
    )
    quotas = (
        QuotaTracker(quota, auth.store, clock=auth.clock, meter_provider=meter_provider)
        if quota.enabled and auth.store is not None and auth.settings.uses_api_keys
        else None
    )
    if limiter is None and quotas is None:
        return None
    audit = rate.throttle_audit
    watch = (
        ThrottleWatch(audit.threshold, audit.window_s, audit.max_tracked)
        if audit.threshold > 0
        else None
    )
    return LimitRuntime(rate, quota, limiter, quotas, watch, auth)


def limits_of(request: Request) -> LimitRuntime | None:
    """The application's limiting components, if any limit is on."""
    return cast("LimitRuntime | None", getattr(request.app.state, "limits", None))


def principal_label(principal: Principal) -> str:
    """The key of a principal's buckets: the API key id, else the principal id."""
    return f"key:{principal.key_id}" if principal.key_id else f"sub:{principal.id}"


async def enforce_limits(request: Request) -> None:
    """Application-wide dependency; see the module documentation."""
    runtime = limits_of(request)
    if runtime is None:
        return
    route = request.scope.get("route")
    path = str(getattr(route, "path", request.url.path))
    if runtime.exempt(path):
        return
    principal = await current_principal(request, get_auth(request))
    if runtime.limiter is not None:
        route_class = runtime.limiter.classify(
            request.method, path, getattr(route, "tags", None) or ()
        )
        domain_id = request.path_params.get("domain_id")
        decision = await runtime.limiter.check(principal_label(principal), route_class, domain_id)
        request.scope.setdefault("state", {})[STATE_KEY] = decision
        if not decision.allowed:
            await _refuse(
                request,
                runtime,
                principal,
                reason=route_class.value,
                problem=ProblemError(
                    RATE_LIMITED,
                    f"Rate limit reached for {route_class.value} requests. "
                    f"Retry in {decision.retry_after_s} s.",
                    extensions={
                        "class": route_class.value,
                        "retry_after_s": decision.retry_after_s,
                    },
                    headers={RETRY_HEADER: str(decision.retry_after_s)},
                ),
            )
    if runtime.quotas is not None and principal.key_id is not None:
        refused, snapshot = await runtime.quotas.admit(principal.key_id)
        if refused is not None:
            await _refuse(
                request,
                runtime,
                principal,
                reason=f"quota:{refused}",
                problem=quota_problem(refused, snapshot, runtime.auth),
            )


def quota_problem(kind: QuotaKind, snapshot: QuotaSnapshot, auth: AuthRuntime) -> ProblemError:
    """429 for a used-up daily quota, with the time until it resets."""
    wait = max(1, math.ceil((snapshot.resets_at - auth.clock()).total_seconds()))
    limit = snapshot.requests_limit if kind == "requests" else snapshot.tokens_limit
    return ProblemError(
        QUOTA_EXCEEDED,
        f"The daily quota of {limit} {kind.replace('_', ' ')} is used up. "
        f"It resets at {snapshot.resets_at.isoformat()}.",
        extensions={
            "quota": kind,
            "limit": limit,
            "resets_at": snapshot.resets_at.isoformat(),
            "retry_after_s": wait,
        },
        headers={RETRY_HEADER: str(wait)},
    )


async def _refuse(
    request: Request,
    runtime: LimitRuntime,
    principal: Principal,
    *,
    reason: str,
    problem: ProblemError,
) -> None:
    """Report the refusal for the audit log and raise it."""
    suppress(request)
    if runtime.watch is not None and (count := runtime.watch.record(principal_label(principal))):
        details: dict[str, Any] = {
            "reason": reason,
            "refusals": count,
            "window_s": runtime.rate.throttle_audit.window_s,
            "method": request.method,
        }
        if principal.key_id:
            details["api_key_id"] = principal.key_id
        domain_id = request.path_params.get("domain_id")
        logger.warning("principal throttled repeatedly", extra={"principal": principal.id})
        writer = runtime.auth.audit_writer
        if writer is not None:
            await writer.write(
                NewAuditEntry(
                    actor=principal.id,
                    action=THROTTLED_ACTION,
                    domain_id=str(domain_id) if domain_id else None,
                    target=principal.key_id,
                    details=details,
                )
            )
    raise problem


async def check_context_quota(request: Request, principal: Principal) -> None:
    """429 when the API key has used up its daily context-pack tokens."""
    runtime = limits_of(request)
    if runtime is None or runtime.quotas is None or principal.key_id is None:
        return
    snapshot = await runtime.quotas.check_tokens(principal.key_id)
    if snapshot is not None:
        await _refuse(
            request,
            runtime,
            principal,
            reason="quota:context_tokens",
            problem=quota_problem("context_tokens", snapshot, runtime.auth),
        )


async def charge_context_tokens(
    request: Request, principal: Principal, response: Response, chars_per_token: float
) -> None:
    """Count the tokens of a delivered context pack against the API key's daily quota."""
    runtime = limits_of(request)
    if runtime is None or runtime.quotas is None or principal.key_id is None:
        return
    if response.status_code != 200:
        return
    tokens = math.ceil(len(response.body) / chars_per_token)
    await runtime.quotas.charge_tokens(principal.key_id, tokens)


class RateLimitHeadersMiddleware:
    """Adds `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` (seconds until the bucket
    is full) to the responses of limited routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                decision = (scope.get("state") or {}).get(STATE_KEY)
                if isinstance(decision, Decision):
                    headers = MutableHeaders(scope=message)
                    headers[LIMIT_HEADER] = str(decision.limit)
                    headers[REMAINING_HEADER] = str(decision.remaining)
                    headers[RESET_HEADER] = str(decision.reset_s)
                    if not decision.allowed:
                        headers[RETRY_HEADER] = str(decision.retry_after_s)
            await send(message)

        await self.app(scope, receive, send_with_headers)


__all__ = [
    "EXPOSED_HEADERS",
    "QUOTA_EXCEEDED",
    "THROTTLED_ACTION",
    "LimitRuntime",
    "RateLimitHeadersMiddleware",
    "RouteClass",
    "build_limits",
    "charge_context_tokens",
    "check_context_quota",
    "enforce_limits",
    "limits_of",
]
