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

    def only_reads(self, method: str, route_path: str) -> bool:
        """Whether a request only reads: GET, HEAD, or a POST search, context or read route
        (the reads recorded by `keeper.auth.audit.reads`)."""
        if method in ("GET", "HEAD"):
            return True
        return method == "POST" and route_path.endswith(
            (*self.search_suffixes, *self.context_suffixes, *self.read_post_suffixes)
        )


class AuthFailureSettings(BucketSettings):
    """Buckets of failed authentications: one per client address and, unless
    `per_key_prefix_enabled` is off, one per API key prefix.

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
    per_key_prefix_enabled: bool = Field(
        default=True,
        description="Keep a bucket per presented API key prefix. Prefixes are not secret, so "
        "whoever knows one can keep that key throttled; turn this off to count failures by "
        "client address only (the per-address bucket is always on).",
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


class PostgresRateSettings(SectionModel):
    """The shared backend (`backend: postgres`): buckets in the UNLOGGED `rate_limit_buckets`
    table of the application database, leased to each Keeper a few tokens at a time."""

    lease_tokens: int = Field(
        default=10,
        ge=1,
        description="Tokens a Keeper takes from a shared bucket per round trip and then serves "
        "from memory; at most a quarter of the bucket (at least 1). Larger leases mean fewer "
        "queries; unused leased tokens are lost when the lease expires, which only makes the "
        "limit stricter. Failed-authentication buckets always take one token at a time.",
    )
    lease_ttl_s: float = Field(
        default=2.0,
        gt=0,
        description="Leased tokens not used within this time are dropped, so an idle Keeper "
        "does not hold a principal's tokens back.",
    )
    timeout_s: float = Field(
        default=0.25,
        gt=0,
        description="Time budget of one query to the shared buckets; slower counts as failed.",
    )
    on_error: Literal["local", "allow", "deny"] = Field(
        default="local",
        description="When the database fails or is slow: `local` enforces the limits in this "
        "process meanwhile (as backend local), `allow` lets the request through, `deny` "
        "answers 429. Failed-authentication buckets always fail closed (429).",
    )
    error_retry_after_s: int = Field(
        default=1,
        ge=1,
        description="Retry-After of a request refused because the shared buckets could not be "
        "read (`deny`, and failed-authentication buckets).",
    )
    check_cache_s: float = Field(
        default=1.0,
        ge=0,
        description="How long a reading of a failed-authentication bucket serves the checks "
        "made before each authentication (which take nothing). Failures themselves are always "
        "written at once.",
    )
    prune_interval_s: float = Field(
        default=60.0,
        gt=0,
        description="How often each Keeper deletes rows of buckets that are full again.",
    )
    prune_batch: int = Field(default=1000, ge=1, description="Rows deleted per prune.")


class RateLimitSettings(SectionModel):
    """Token buckets per principal and request class (design 13.6)."""

    enabled: bool = Field(default=True, description="Apply rate limits.")
    backend: Literal["local", "memory", "postgres"] = Field(
        default="local",
        description="Where bucket state lives. `local` (`memory` is an alias) is per process: "
        "with N Keeper processes a principal may use up to N times the limits. `postgres` "
        "shares the buckets of every Keeper through the application database (see "
        "`postgres`); use it with more than one Keeper.",
    )
    postgres: PostgresRateSettings = Field(default_factory=PostgresRateSettings)
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
