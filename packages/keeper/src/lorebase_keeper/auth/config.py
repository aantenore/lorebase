"""The `keeper.auth` settings section: authentication, authorization and audit.

Every value has a documented default; deployments override it in the YAML file or with
`LOREBASE__KEEPER__AUTH__...` variables. See `config/lorebase.example.yaml`.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from lorebase_keeper.auth.permissions import BUILTIN_ROLES, permission_names
from lorebase_platform.secrets import SecretValue
from lorebase_platform.settings import SectionModel

AuthMode = Literal["none", "api_key", "oidc", "api_key+oidc"]

ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
"""Signature algorithms accepted for tokens. Shared-secret algorithms are excluded on purpose:
a provider's keys are public, so accepting HS* would let anyone sign a token."""

MIN_PEPPER_LENGTH = 32
_TOKEN_PREFIX = re.compile(r"^[a-z][a-z0-9]{1,15}$")
_ROLE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def is_loopback(host: str) -> bool:
    """Whether a host name or address only reaches this machine."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def insecure_url_problem(url: str, allow_insecure: bool) -> str | None:
    """Why `url` may not be used to reach an identity provider, or None when it may.

    Signing keys fetched over plain HTTP can be replaced in transit, so provider URLs must use
    https; http is accepted for loopback hosts or when `allow_insecure` is set.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "must be an absolute https URL"
    if parts.scheme == "http" and not allow_insecure and not is_loopback(parts.hostname):
        return "must use https (http is accepted for loopback hosts or with oidc.allow_insecure)"
    return None


MAX_LOCAL_PATH_LENGTH = 2048


def is_local_path(value: str) -> bool:
    """Whether `value` is a path on the current origin, safe to redirect to.

    It must start with a single `/` and hold no backslash, control character, scheme or
    authority: `//evil.example`, `/\\evil.example` and `https://...` are refused, since browsers
    read them as other origins.
    """
    if not value.startswith("/") or value.startswith("//") or len(value) > MAX_LOCAL_PATH_LENGTH:
        return False
    if "\\" in value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return False
    parts = urlsplit(value)
    return not parts.scheme and not parts.netloc


class ApiKeySettings(SectionModel):
    """API keys for agents and automation (design 13.2)."""

    token_prefix: str = Field(
        default="lbk",
        description="First part of every key: <token_prefix>_<prefix>_<secret>. Changing it "
        "invalidates keys already issued.",
    )
    header: str = Field(
        default="X-Api-Key",
        min_length=1,
        description="Header that may carry a key instead of `Authorization: Bearer`.",
    )
    pepper: SecretValue | None = Field(
        default=None,
        description="Server-side key for the HMAC-SHA256 stored for each key secret; required "
        "when API keys are enabled, at least 32 characters, e.g. secret://env/LOREBASE_PEPPER. "
        "Changing it invalidates every issued key.",
    )
    prefix_length: int = Field(
        default=10, ge=6, le=32, description="Characters of the public lookup prefix."
    )
    secret_bytes: int = Field(
        default=32, ge=24, le=128, description="Random bytes of the secret part."
    )
    default_ttl_days: int | None = Field(
        default=365,
        ge=1,
        description="Lifetime of new keys when the request gives none; null: keys do not expire.",
    )
    max_ttl_days: int | None = Field(
        default=None,
        ge=1,
        description="Longest lifetime an administrator may request; null: no limit. When set, "
        "keys that never expire cannot be created.",
    )
    last_used_interval_s: float = Field(
        default=60,
        ge=0,
        description="A key's last-used time is written at most this often; 0 writes on every use.",
    )
    max_allowed_networks: int = Field(
        default=32,
        ge=1,
        description="Most networks an administrator may list in a key's `allowed_networks`.",
    )

    @field_validator("token_prefix")
    @classmethod
    def _token_prefix_shape(cls, value: str) -> str:
        if not _TOKEN_PREFIX.match(value):
            raise ValueError("must be 2 to 16 lowercase letters or digits, starting with a letter")
        return value

    @model_validator(mode="after")
    def _default_within_max(self) -> Self:
        if self.max_ttl_days is not None and (
            self.default_ttl_days is None or self.default_ttl_days > self.max_ttl_days
        ):
            raise ValueError("default_ttl_days must be set and not exceed max_ttl_days")
        return self


class OidcSettings(SectionModel):
    """Bearer token validation against any OpenID Connect provider (design 13.2)."""

    issuer: str | None = Field(
        default=None, description="Expected `iss`; also the base of the discovery document."
    )
    audience: tuple[str, ...] = Field(
        default=(),
        description="Accepted `aud` values (a single string is accepted); a token must carry "
        "at least one.",
    )
    jwks_uri: str | None = Field(
        default=None,
        description="Signing keys of the provider. Null: taken from "
        "<issuer>/.well-known/openid-configuration.",
    )
    algorithms: tuple[str, ...] = Field(
        default=("RS256", "ES256"), min_length=1, description="Accepted signature algorithms."
    )
    clock_skew_s: float = Field(
        default=60, ge=0, le=600, description="Tolerance for exp, nbf and iat."
    )
    jwks_cache_ttl_s: float = Field(
        default=300, gt=0, description="How long fetched signing keys are used before a refresh."
    )
    jwks_min_refresh_s: float = Field(
        default=30,
        ge=0,
        description="Shortest time between refreshes triggered by an unknown key id.",
    )
    http_timeout_s: float = Field(default=5, gt=0, description="Timeout of provider requests.")
    subject_claim: str = Field(default="sub", min_length=1)
    name_claims: tuple[str, ...] = Field(
        default=("name", "preferred_username", "email"),
        description="Claims tried in order for the display name; the subject is the fallback.",
    )
    groups_claim: str = Field(
        default="groups", min_length=1, description="Claim listing the provider groups."
    )
    auto_provision: bool = Field(
        default=True,
        description="Create the user principal on the first valid token. When false, only "
        "existing principals can sign in.",
    )
    principal_cache_ttl_s: float = Field(
        default=60, ge=0, description="How long a token subject's principal and groups are reused."
    )
    principal_cache_max_entries: int = Field(default=4096, ge=1)
    allow_insecure: bool = Field(
        default=False,
        description="Accept http `issuer` and `jwks_uri` values (configured or discovered) for "
        "hosts other than loopback. Signing keys fetched over http can be replaced in transit; "
        "for development only.",
    )

    @field_validator("audience", mode="before")
    @classmethod
    def _audience_may_be_a_string(cls, value: Any) -> Any:
        return (value,) if isinstance(value, str) else value

    @field_validator("algorithms")
    @classmethod
    def _asymmetric_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unsupported = sorted(set(value) - ASYMMETRIC_ALGORITHMS)
        if unsupported:
            raise ValueError(
                f"unsupported {unsupported}; use asymmetric algorithms: "
                f"{', '.join(sorted(ASYMMETRIC_ALGORITHMS))}"
            )
        return value

    @model_validator(mode="after")
    def _secure_provider_urls(self) -> Self:
        for name, url in (("issuer", self.issuer), ("jwks_uri", self.jwks_uri)):
            problem = None if url is None else insecure_url_problem(url, self.allow_insecure)
            if problem:
                raise ValueError(f"oidc.{name} {url!r} {problem}")
        return self


TokenEndpointAuth = Literal["client_secret_basic", "client_secret_post", "none"]
MIN_SESSION_KEY_LENGTH = 32
_COOKIE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def url_origin(url: str) -> str:
    """`scheme://host[:port]` of an absolute URL, lower-cased as browsers send it."""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


class SessionSettings(SectionModel):
    """Sign-in of the web app: Keeper as backend-for-frontend (design 13.2).

    Keeper runs the OpenID Connect authorization code flow with PKCE against the provider of
    `keeper.auth.oidc`, keeps the provider tokens server-side and gives the browser an opaque
    session cookie. Needs an `oidc` auth mode and a database.
    """

    enabled: bool = Field(
        default=False,
        description="Offer browser sign-in (<api_prefix>/auth/login) and accept session cookies.",
    )
    discovery_url: str | None = Field(
        default=None,
        description="Provider metadata. Null: <oidc.issuer>/.well-known/openid-configuration. "
        "The document's issuer must equal oidc.issuer.",
    )
    client_id: str | None = Field(
        default=None, description="Client id registered at the provider; REQUIRED when enabled."
    )
    client_secret: SecretValue | None = Field(
        default=None,
        description="Client secret, e.g. secret://env/LOREBASE_OIDC_CLIENT_SECRET; required "
        "unless token_endpoint_auth_method is none (a public client relying on PKCE alone).",
    )
    token_endpoint_auth_method: TokenEndpointAuth = Field(
        default="client_secret_basic",
        description="How Keeper authenticates at the token endpoint: client_secret_basic | "
        "client_secret_post | none.",
    )
    scopes: tuple[str, ...] = Field(
        default=("openid", "profile", "email"),
        min_length=1,
        description="Requested scopes; must include openid. Add offline_access when the "
        "provider issues refresh tokens only with it.",
    )
    redirect_uri: str | None = Field(
        default=None,
        description="Callback registered at the provider, sent unchanged (exact match): the "
        "public URL of <api_prefix>/auth/callback, e.g. "
        "https://lorebase.example.com/api/v1/auth/callback. REQUIRED when enabled.",
    )
    app_url: str | None = Field(
        default=None,
        description="Public URL of the web app, e.g. https://lorebase.example.com/. Sign-in "
        "returns to a path on its origin; REQUIRED when enabled.",
    )
    post_logout_redirect_uri: str | None = Field(
        default=None,
        description="Where the provider sends the browser after sign-out (registered at the "
        "provider). Null: app_url.",
    )
    error_path: str = Field(
        default="/login",
        description="Path on the app's origin that receives failed sign-ins as ?error=<code>.",
    )
    encryption_key: SecretValue | None = Field(
        default=None,
        description="Key (at least 32 characters) that encrypts the sign-in state cookie and the "
        "provider tokens stored with a session, e.g. secret://env/LOREBASE_SESSION_KEY. "
        "REQUIRED when enabled. Changing it voids sign-ins in progress and ends each session "
        "at its next token refresh.",
    )
    cookie_name: str = Field(
        default="lorebase_session",
        description="Session cookie name; with cookie_secure the browser gets __Host-<name>.",
    )
    login_cookie_name: str = Field(
        default="lorebase_login",
        description="Short-lived cookie holding the encrypted state, nonce and PKCE verifier "
        "of a sign-in in progress; __Host- prefixed with cookie_secure.",
    )
    cookie_secure: bool = Field(
        default=True,
        description="Mark cookies Secure and use the __Host- prefix. false only for plain http "
        "development on loopback.",
    )
    same_site: Literal["lax", "strict"] = Field(
        default="lax", description="SameSite attribute of the session cookie."
    )
    login_timeout_s: int = Field(
        default=600, ge=30, le=3600, description="Time allowed to finish a sign-in."
    )
    idle_timeout_s: int = Field(default=3600, ge=60, description="A session unused this long ends.")
    absolute_timeout_s: int = Field(
        default=43200, ge=60, description="A session ends this long after sign-in, used or not."
    )
    touch_interval_s: float = Field(
        default=60,
        ge=0,
        description="A session's last activity is written at most this often; the idle timeout "
        "is exact to within this interval.",
    )
    refresh_tokens: bool = Field(
        default=True,
        description="Keep the provider's refresh token and use it when the access token expires: "
        "a refused refresh (account disabled, consent revoked) ends the session and updated "
        "group claims apply. false: the session lives by its timeouts only.",
    )
    refresh_default_interval_s: int = Field(
        default=300,
        ge=1,
        description="Time to the next refresh when the token response gives no expires_in.",
    )
    refresh_min_interval_s: int = Field(
        default=60, ge=1, description="Shortest time between two refreshes of a session."
    )
    refresh_lease_s: int = Field(
        default=30,
        ge=1,
        description="One request refreshes a session at a time; after a failure the next attempt "
        "waits this long. Other requests proceed with the session meanwhile.",
    )
    metadata_cache_ttl_s: float = Field(
        default=3600, gt=0, description="How long the provider metadata document is reused."
    )
    csrf_header: str = Field(
        default="X-CSRF-Token",
        description="Header carrying the session's CSRF token on POST, PUT, PATCH and DELETE.",
    )
    allowed_origins: tuple[str, ...] = Field(
        default=(),
        description="Origins accepted in the Origin header of unsafe requests besides those of "
        "app_url and redirect_uri.",
    )
    purge_batch: int = Field(
        default=100,
        ge=0,
        description="Expired sessions deleted at each sign-in; 0 leaves them in the table.",
    )

    @field_validator("cookie_name", "login_cookie_name")
    @classmethod
    def _cookie_name_shape(cls, value: str) -> str:
        if not _COOKIE_NAME.match(value) or value.startswith(("__Host-", "__Secure-")):
            raise ValueError("must be 1 to 64 letters, digits, '_' or '-', without a prefix")
        return value

    @field_validator("csrf_header")
    @classmethod
    def _header_shape(cls, value: str) -> str:
        if not _HEADER_NAME.match(value):
            raise ValueError("must be a header name of letters, digits and '-'")
        return value

    @field_validator("error_path")
    @classmethod
    def _path_shape(cls, value: str) -> str:
        if not is_local_path(value):
            raise ValueError("must be a path starting with a single '/'")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def _origins_shape(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for origin in value:
            parts = urlsplit(origin)
            if parts.scheme not in ("http", "https") or not parts.netloc or parts.path:
                raise ValueError(f"{origin!r} is not an origin (scheme://host[:port])")
        return tuple(url_origin(origin) for origin in value)

    @property
    def secure_cookie_name(self) -> str:
        """Name of the session cookie as sent to the browser."""
        return f"__Host-{self.cookie_name}" if self.cookie_secure else self.cookie_name

    @property
    def secure_login_cookie_name(self) -> str:
        """Name of the sign-in state cookie as sent to the browser."""
        return f"__Host-{self.login_cookie_name}" if self.cookie_secure else self.login_cookie_name

    def trusted_origins(self) -> frozenset[str]:
        """Origins accepted in the Origin header of unsafe cookie-authenticated requests."""
        origins = set(self.allowed_origins)
        for url in (self.app_url, self.redirect_uri):
            if url:
                origins.add(url_origin(url))
        return frozenset(origins)

    def problems(self, allow_insecure: bool) -> list[str]:
        """What keeps an enabled configuration from working (empty when it is usable)."""
        problems = [
            f"sessions.{name} is required when sessions are enabled"
            for name in ("client_id", "redirect_uri", "app_url")
            if not getattr(self, name)
        ]
        key = self.encryption_key
        if key is None or len(key.get_secret_value()) < MIN_SESSION_KEY_LENGTH:
            problems.append(
                "sessions.encryption_key is required when sessions are enabled "
                f"(at least {MIN_SESSION_KEY_LENGTH} characters)"
            )
        if self.token_endpoint_auth_method != "none" and self.client_secret is None:
            problems.append(
                "sessions.client_secret is required with token_endpoint_auth_method "
                f"{self.token_endpoint_auth_method!r}"
            )
        if "openid" not in self.scopes:
            problems.append("sessions.scopes must include openid")
        if self.idle_timeout_s > self.absolute_timeout_s:
            problems.append("sessions.idle_timeout_s cannot exceed absolute_timeout_s")
        for name in ("discovery_url", "redirect_uri", "app_url", "post_logout_redirect_uri"):
            url = getattr(self, name)
            problem = None if url is None else insecure_url_problem(url, allow_insecure)
            if problem:
                problems.append(f"sessions.{name} {url!r} {problem}")
        return problems


class AuthzSettings(SectionModel):
    """Authorization: grant caching and extra roles."""

    grants_cache_ttl_s: float = Field(
        default=15,
        ge=0,
        description="How long a principal's grants in a Domain are reused. Grant changes made "
        "through another Keeper process take effect after at most this long; 0 disables.",
    )
    grants_cache_max_entries: int = Field(default=2048, ge=1)
    invisible_kinds: bool = Field(
        default=True,
        description="Kinds a caller's Kind-narrowed grants leave out are invisible to it, not "
        "just refused: the schema it reads omits them and what tells about them, and requests "
        "naming them answer like unknown names (404 or 422, never 403). False refuses them "
        "with 403 naming the Kind and refuses the schema to such callers.",
    )
    custom_roles: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description="Extra roles: name -> permissions (see docs for the names). Built-in roles "
        "cannot be redefined. Example: {contributor: [schema.read, data.read, branch.create]}.",
    )

    @field_validator("custom_roles")
    @classmethod
    def _valid_roles(cls, roles: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        known = permission_names()
        for name, permissions in roles.items():
            if not _ROLE_NAME.match(name):
                raise ValueError(f"role name {name!r} must match {_ROLE_NAME.pattern}")
            if name in BUILTIN_ROLES:
                raise ValueError(f"role {name!r} is built in and cannot be redefined")
            unknown = sorted(set(permissions) - known)
            if unknown:
                raise ValueError(f"role {name!r} has unknown permissions {unknown}")
        return roles


class ReadAuditSettings(SectionModel):
    """Audit of reads (design 13.6): one record per successful read, sampled and filtered,
    written in batches to the read stream of the audit log (`audit_read_log`, chains of its
    own), off the request path.

    A read that showed or masked a value of a sensitive Attribute (`pii`, `confidential`,
    `restricted`, vault values included) is always recorded, whatever the sampling and the
    filters. Records hold who, which route and Domain, the Kind when the route names one and
    the outcome; never query strings, bodies or values.
    """

    enabled: bool = Field(default=False, description="Record reads. Needs the `database` section.")
    sample_rate: float = Field(
        default=1.0,
        ge=0,
        le=1,
        description="Share of the reads that pass the filters recorded (0 to 1). Sensitive "
        "reads are always recorded.",
    )
    principal_types: tuple[Literal["user", "service", "api_key"], ...] = Field(
        default=(),
        description="Record only reads by these principal types; empty records every type.",
    )
    kinds: tuple[str, ...] = Field(
        default=(),
        description="Record only reads of these Kinds (api names, as routes name them); reads "
        "of routes that name no Kind are then left out. Empty records every read.",
    )
    queue_size: int = Field(
        default=10_000,
        ge=1,
        description="Records waiting to be written; past it new records are dropped and "
        "counted in `lorebase.audit.reads.dropped` (reason `queue_full`).",
    )
    batch_size: int = Field(
        default=500, ge=1, description="Records written per transaction at most."
    )
    flush_interval_s: float = Field(
        default=1.0,
        gt=0,
        description="Longest wait before records in the queue are written.",
    )
    write_timeout_s: float = Field(
        default=5.0,
        gt=0,
        description="Time budget of one batch; with `on_overflow: drop` a batch that fails is "
        "dropped and counted (reason `write_failed`).",
    )
    on_overflow: Literal["drop", "reject"] = Field(
        default="drop",
        description="What happens when a record cannot be kept. `drop`: the read is served and "
        "the record dropped and counted. `reject`: a read is answered only once its record "
        "holds a place in the queue (else 503 with `Retry-After`), and a batch that fails is "
        "kept and retried, never dropped; for deployments where the read stream is proof.",
    )
    retry_after_s: int = Field(
        default=1,
        ge=1,
        description="Retry-After of a read refused because the queue is full "
        "(`on_overflow: reject`).",
    )
    unready_after_failures: int = Field(
        default=3,
        ge=1,
        description="Failed attempts in a row of one batch after which readiness reports the "
        "read audit as failed until a batch is written again (`on_overflow: reject`).",
    )


class AuditSettings(SectionModel):
    """Audit log of mutations and denied requests (design 10.4), and optionally of reads."""

    enabled: bool = Field(
        default=True,
        description="Write the audit log. Takes effect when a database is configured.",
    )
    record_denied: bool = Field(
        default=True,
        description="Also record requests rejected with 401 or 403.",
    )
    write_timeout_s: float = Field(
        default=2.0,
        gt=0,
        description="Time budget of one audit write. A failed write is logged and counted, "
        "never surfaced to the client.",
    )
    reads: ReadAuditSettings = Field(default_factory=ReadAuditSettings)


class AuthSettings(SectionModel):
    """Authentication and authorization of the API."""

    mode: AuthMode = Field(
        default="none",
        description="none | api_key | oidc | api_key+oidc. `none` accepts every request as an "
        "administrator and is for local development only.",
    )
    allow_insecure: bool = Field(
        default=False,
        description="Allow mode `none` on a bind address other than loopback (`keeper.host`). "
        "Without it such a configuration is refused at start.",
    )
    installation_admins: tuple[str, ...] = Field(
        default=(),
        description="Principals allowed to administer the installation (list principals). "
        "Each entry is a principal id, an identity provider subject or a group id; it grants "
        "no access to Domain data.",
    )
    api_keys: ApiKeySettings = Field(default_factory=ApiKeySettings)
    oidc: OidcSettings = Field(default_factory=OidcSettings)
    authz: AuthzSettings = Field(default_factory=AuthzSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    sessions: SessionSettings = Field(default_factory=SessionSettings)

    @property
    def uses_api_keys(self) -> bool:
        """API keys are accepted."""
        return self.mode in ("api_key", "api_key+oidc")

    @property
    def uses_oidc(self) -> bool:
        """OIDC bearer tokens are accepted."""
        return self.mode in ("oidc", "api_key+oidc")

    @model_validator(mode="after")
    def _mode_requirements(self) -> Self:
        if self.uses_api_keys:
            pepper = self.api_keys.pepper
            if pepper is None or len(pepper.get_secret_value()) < MIN_PEPPER_LENGTH:
                raise ValueError(
                    f"api_keys.pepper is required with mode {self.mode!r} "
                    f"(at least {MIN_PEPPER_LENGTH} characters)"
                )
        if self.uses_oidc and not (self.oidc.issuer and self.oidc.audience):
            raise ValueError(f"oidc.issuer and oidc.audience are required with mode {self.mode!r}")
        if self.sessions.enabled:
            if not self.uses_oidc:
                raise ValueError(f"sessions need mode oidc or api_key+oidc, not {self.mode!r}")
            problems = self.sessions.problems(self.oidc.allow_insecure)
            if problems:
                raise ValueError("; ".join(problems))
        return self
