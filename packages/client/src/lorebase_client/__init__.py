"""Lorebase client: typed sync and async HTTP clients for the Keeper API.

    from lorebase_client import LorebaseClient

    client = LorebaseClient()  # LOREBASE_URL, LOREBASE_API_KEY or LOREBASE_TOKEN
    print(client.whoami())

The package depends on `httpx` and `pydantic` only.
"""

from lorebase_client.aclient import AsyncLorebaseClient
from lorebase_client.client import LorebaseClient
from lorebase_client.config import (
    ENV_API_KEY,
    ENV_API_PREFIX,
    ENV_TIMEOUT,
    ENV_TOKEN,
    ENV_URL,
    ClientConfig,
    RetryPolicy,
    resolve_config,
)
from lorebase_client.core import Call, ETagCache, new_idempotency_key
from lorebase_client.errors import (
    AlreadyExistsError,
    ApiError,
    AuthenticationError,
    BadRequestError,
    BranchBusyError,
    ConflictError,
    IdempotencyConflictError,
    LorebaseConfigError,
    LorebaseConnectionError,
    LorebaseError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    ProtectedBranchError,
    RateLimitedError,
    ReviewRequiredError,
    ServerError,
    StaleHeadError,
    UnavailableError,
    ValidationFailedError,
    error_from_response,
)

__version__ = "0.2.3"

__all__ = [
    "ENV_API_KEY",
    "ENV_API_PREFIX",
    "ENV_TIMEOUT",
    "ENV_TOKEN",
    "ENV_URL",
    "AlreadyExistsError",
    "ApiError",
    "AsyncLorebaseClient",
    "AuthenticationError",
    "BadRequestError",
    "BranchBusyError",
    "Call",
    "ClientConfig",
    "ConflictError",
    "ETagCache",
    "IdempotencyConflictError",
    "LorebaseClient",
    "LorebaseConfigError",
    "LorebaseConnectionError",
    "LorebaseError",
    "NotFoundError",
    "PayloadTooLargeError",
    "PermissionDeniedError",
    "ProtectedBranchError",
    "RateLimitedError",
    "RetryPolicy",
    "ReviewRequiredError",
    "ServerError",
    "StaleHeadError",
    "UnavailableError",
    "ValidationFailedError",
    "__version__",
    "error_from_response",
    "new_idempotency_key",
    "resolve_config",
]
