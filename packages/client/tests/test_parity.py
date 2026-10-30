"""The sync and async clients expose the same API.

`client.py` and `aclient.py` are kept in step by hand: every resource class, public method and
signature of one must exist in the other. Only the async forms differ: coroutines instead of
functions, `AsyncIterator` instead of `Iterator`, and the client's `aclose` instead of `close`.
"""

from __future__ import annotations

import inspect
from types import ModuleType
from typing import Any

import pytest

from lorebase_client import aclient, client

RENAMED = {"Client": {"close": "aclose"}}
"""Per class, sync method names whose async counterpart has another name."""


def classes(module: ModuleType) -> dict[str, type[Any]]:
    """Classes defined in `module`, the client class under a shared name."""
    found = {
        name: value
        for name, value in vars(module).items()
        if inspect.isclass(value) and value.__module__ == module.__name__
    }
    found["Client"] = found.pop("AsyncLorebaseClient" if module is aclient else "LorebaseClient")
    return found


def public_methods(cls: type[Any]) -> dict[str, Any]:
    return {
        name: value
        for name, value in vars(cls).items()
        if not name.startswith("_") and callable(value)
    }


def signature(function: Any) -> str:
    """The signature with the async-only spelling of the return type made sync."""
    return str(inspect.signature(function)).replace("AsyncIterator[", "Iterator[")


SYNC, ASYNC = classes(client), classes(aclient)


def test_both_clients_define_the_same_classes() -> None:
    assert set(SYNC) == set(ASYNC)


@pytest.mark.parametrize("name", sorted(SYNC))
def test_methods_and_signatures_match(name: str) -> None:
    sync = public_methods(SYNC[name])
    asynchronous = public_methods(ASYNC[name])
    renamed = RENAMED.get(name, {})
    expected = {renamed.get(method, method) for method in sync}
    assert expected == set(asynchronous), f"{name}: {expected ^ set(asynchronous)}"
    for method, function in sync.items():
        counterpart = asynchronous[renamed.get(method, method)]
        assert signature(function) == signature(counterpart), f"{name}.{method}"
