"""The `keeper.auth` settings section: authentication, authorization and audit.

Every value has a documented default; deployments override it in the YAML file or with
`LOREBASE__KEEPER__AUTH__...` variables. See `config/lorebase.example.yaml`.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Self

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


class AuthzSettings(SectionModel):
    """Authorization: grant caching and extra roles."""

    grants_cache_ttl_s: float = Field(
        default=15,
        ge=0,
        description="How long a principal's grants in a Domain are reused. Grant changes made "
        "through another Keeper process take effect after at most this long; 0 disables.",
    )
    grants_cache_max_entries: int = Field(default=2048, ge=1)
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


class AuditSettings(SectionModel):
    """Audit log of mutations and denied requests (design 10.4)."""

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


class AuthSettings(SectionModel):
    """Authentication and authorization of the API."""

    mode: AuthMode = Field(
        default="none",
        description="none | api_key | oidc | api_key+oidc. `none` accepts every request as an "
        "administrator and is for local development only.",
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
        return self
