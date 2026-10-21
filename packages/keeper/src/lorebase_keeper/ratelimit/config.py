"""Settings of rate limiting and daily quotas (`keeper.limits.rate`, `keeper.limits.quota`).

Every value has a documented default; deployments override it in the YAML file or with
`LOREBASE__KEEPER__LIMITS__RATE__...` variables. See `config/lorebase.example.yaml`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from lorebase_platform.settings import SectionModel


class RouteClass(StrEnum):
    """What a request costs, as far as limits are concerned."""

    READS = "reads"
    WRITES = "writes"
    SEARCHES = "searches"
    CONTEXT = "context"
    ADMIN = "admin"


class BucketSettings(SectionModel):
    """One token bucket: it refills at `requests_per_minute` and holds at most `burst` tokens."""

    requests_per_minute: float = Field(gt=0, description="Sustained rate: tokens added per minute.")
    burst: int = Field(ge=1, description="Bucket size: requests allowed back to back.")


def _bucket(requests_per_minute: float, burst: int) -> BucketSettings:
    return BucketSettings(requests_per_minute=requests_per_minute, burst=burst)


class ClassOverrides(SectionModel):
    """Buckets of one Domain that replace the installation-wide ones; unset classes keep them."""

    reads: BucketSettings | None = None
    writes: BucketSettings | None = None
    searches: BucketSettings | None = None
    context: BucketSettings | None = None
    admin: BucketSettings | None = None

    def get(self, route_class: RouteClass) -> BucketSettings | None:
        """The override of a class, if any."""
        found: BucketSettings | None = getattr(self, route_class.value)
        return found


class RouteClassing(SectionModel):
    """How requests are sorted into classes. Anything not matched is `reads` for GET, HEAD and
    OPTIONS and `writes` for every other method."""

    admin_tags: tuple[str, ...] = Field(
        default=("admin",), description="Routes with one of these OpenAPI tags are `admin`."
    )
    search_suffixes: tuple[str, ...] = Field(
        default=("/search",), description="POST routes ending in one of these are `searches`."
    )
    context_suffixes: tuple[str, ...] = Field(
        default=("/context",), description="POST routes ending in one of these are `context`."
    )
    read_post_suffixes: tuple[str, ...] = Field(
        default=("/query", "/traverse", "/neighborhood"),
        description="POST routes ending in one of these only read: they are `reads`.",
    )


class AuthFailureSettings(BucketSettings):
    """Buckets of failed authentications: one per client address and one per API key prefix.

    Only requests answered 401 after presenting a credential take a token. While either bucket
    of a request is empty it is answered 429 before its credential is checked.
    """

    requests_per_minute: float = Field(
        default=30, gt=0, description="Failed attempts regained per minute."
    )
    burst: int = Field(default=30, ge=1, description="Failed attempts allowed back to back.")
    ipv6_prefix_length: int = Field(
        default=64,
        ge=1,
        le=128,
        description="IPv6 clients share the bucket of their network of this length, since one "
        "client usually holds a whole /64.",
    )


class ThrottleAuditSettings(SectionModel):
    """Audit entry for a principal that keeps being throttled."""

    threshold: int = Field(
        default=20,
        ge=0,
        description="Refusals within `window_s` that write one audit entry (`rate_limit."
        "throttled`); the count then starts again. 0 disables.",
    )
    window_s: float = Field(default=300, gt=0, description="Length of the counting window.")
    max_tracked: int = Field(default=10_000, ge=1, description="Principals counted at once.")


class RateLimitSettings(SectionModel):
    """Token buckets per principal and request class (design 13.6)."""

    enabled: bool = Field(default=True, description="Apply rate limits.")
    backend: Literal["memory"] = Field(
        default="memory",
        description="Where bucket state lives. `memory` is per process: with N Keeper "
        "processes a principal may use up to N times the limits. A shared store (Valkey or "
        "Redis) implements `lorebase_keeper.ratelimit.RateStore` and joins this list.",
    )
    max_buckets: int = Field(
        default=100_000, ge=1, description="Buckets kept in memory; the least recently used go."
    )
    exempt_paths: tuple[str, ...] = Field(
        default=("/healthz", "/readyz"),
        description="Route paths (without the API prefix) that are never limited or counted.",
    )
    reads: BucketSettings = Field(default_factory=lambda: _bucket(1200, 200))
    writes: BucketSettings = Field(default_factory=lambda: _bucket(300, 60))
    searches: BucketSettings = Field(default_factory=lambda: _bucket(300, 60))
    context: BucketSettings = Field(default_factory=lambda: _bucket(120, 30))
    admin: BucketSettings = Field(default_factory=lambda: _bucket(120, 30))
    auth_failures: AuthFailureSettings = Field(
        default_factory=AuthFailureSettings,
        description="Failed authentications per client address and per API key prefix.",
    )
    domains: dict[str, ClassOverrides] = Field(
        default_factory=dict,
        description="Per-Domain overrides by Domain id; requests in these Domains use their own "
        "buckets (not shared with the principal's other Domains).",
    )
    routes: RouteClassing = Field(default_factory=RouteClassing)
    throttle_audit: ThrottleAuditSettings = Field(default_factory=ThrottleAuditSettings)

    def bucket(self, route_class: RouteClass, domain_id: str | None = None) -> BucketSettings:
        """The bucket for a class, from the Domain's overrides when it has one."""
        override = self.domains.get(domain_id) if domain_id else None
        found = override.get(route_class) if override else None
        if found is not None:
            return found
        installation: BucketSettings = getattr(self, route_class.value)
        return installation

    def has_override(self, route_class: RouteClass, domain_id: str | None) -> bool:
        """Whether the Domain overrides the bucket of this class."""
        override = self.domains.get(domain_id) if domain_id else None
        return override is not None and override.get(route_class) is not None


class KeyQuota(SectionModel):
    """Quota of one API key; a member left out keeps the installation-wide value, `null`
    removes the limit."""

    requests_per_day: int | None = Field(default=None, ge=1)
    context_tokens_per_day: int | None = Field(default=None, ge=1)


class QuotaSettings(SectionModel):
    """Daily quotas per API key, counted in UTC days (design 13.6)."""

    enabled: bool = Field(
        default=True, description="Count the use of API keys. Needs a database; users are exempt."
    )
    requests_per_day: int | None = Field(
        default=100_000, ge=1, description="Requests per key and day; null: unlimited."
    )
    context_tokens_per_day: int | None = Field(
        default=2_000_000,
        ge=1,
        description="Estimated tokens of context packs per key and day; null: unlimited. A pack "
        "that starts under the limit is delivered whole, so a day may end slightly above it.",
    )
    flush_interval_s: float = Field(
        default=5,
        gt=0,
        description="How often a process writes the use it has counted. Several processes "
        "enforce a quota on what they know, so a key may exceed it by the use of one interval.",
    )
    keys: dict[str, KeyQuota] = Field(
        default_factory=dict,
        description="Overrides by API key id, as listed by the Domain's API key endpoint.",
    )

    def requests_limit(self, key_id: str) -> int | None:
        """Requests per day allowed to a key."""
        return self._limit(key_id, "requests_per_day")

    def tokens_limit(self, key_id: str) -> int | None:
        """Context-pack tokens per day allowed to a key."""
        return self._limit(key_id, "context_tokens_per_day")

    def _limit(self, key_id: str, member: str) -> int | None:
        override = self.keys.get(key_id)
        if override is not None and member in override.model_fields_set:
            value: int | None = getattr(override, member)
            return value
        default: int | None = getattr(self, member)
        return default
