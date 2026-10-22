"""Keys derived from `keeper.auth.sessions.encryption_key` (`lorebase_store.keyring`)."""

from __future__ import annotations

from lorebase_store.keyring import Keyring as _Keyring

NAMESPACE = "keeper"


class Keyring(_Keyring):
    """The session keyring: keys derived under the `keeper` namespace."""

    def __init__(self, secret: str) -> None:
        super().__init__(secret, namespace=NAMESPACE)
