"""Reads are authorized against the Revision they resolve to, not the name the caller gave."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

import pytest

from lorebase_keeper.auth.config import AuthzSettings
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext, Authorizer
from lorebase_keeper.context import StoreAccess
from lorebase_keeper.errors import NOT_FOUND, ProblemError
from lorebase_keeper.services.reads import (
    ReadGuard,
    ReadTarget,
    authorize_read,
    reachable,
    resolve_readable,
)
from lorebase_keeper.settings import ReadsSettings
from lorebase_store.models.enums import GrantScope, PrincipalType, RefKind

READ = (Permission.SCHEMA_READ,)
BOTH = (Permission.SCHEMA_READ, Permission.DATA_READ)


def rid(n: int) -> bytes:
    return bytes([n]) * 32


M1, M2, M3, A1, A2, B1, X, UNKNOWN = (rid(n) for n in (1, 2, 3, 4, 5, 6, 7, 99))
MERGE = rid(8)

# main: M1 <- M2 <- M3    agent/a: M1 <- A1 <- A2    agent/b: M1 <- B1    X: unreachable root
PARENTS = {M1: (), M2: (M1,), M3: (M2,), A1: (M1,), A2: (A1,), B1: (M1,), X: ()}
GENERATIONS = {M1: 1, M2: 2, M3: 3, A1: 2, A2: 3, B1: 2, X: 1}
BRANCHES = {"main": M3, "agent/a": A2, "agent/b": B1}
TAGS = {"v-m3": M3, "v-a1": A1}


class FakeSource:
    """The history above, with the lookup rules of `context.resolve_revision`."""

    def __init__(self, parents: Mapping[bytes, tuple[bytes, ...]] = PARENTS) -> None:
        self.history = dict(parents)
        self.generation = dict(GENERATIONS)
        self.parent_queries = 0

    async def lookup(self, *, branch: str | None, at: str | None) -> ReadTarget:
        if at is not None:
            if len(at) == 64 and all(c in "0123456789abcdef" for c in at):
                found = bytes.fromhex(at)
                if found not in self.history:
                    raise ProblemError(NOT_FOUND, "Revision not found.")
                return ReadTarget(found)
            if at in BRANCHES:
                return ReadTarget(BRANCHES[at], at, RefKind.BRANCH)
            if at in TAGS:
                return ReadTarget(TAGS[at], at, RefKind.TAG)
            raise ProblemError(NOT_FOUND, f"No Revision, tag or branch named {at!r}.")
        name = branch or "main"
        if name not in BRANCHES:
            raise ProblemError(NOT_FOUND, f"Branch {name!r} not found.")
        return ReadTarget(BRANCHES[name], name, RefKind.BRANCH)

    async def branch_heads(self) -> Mapping[str, bytes]:
        return BRANCHES

    async def parents(self, revision: bytes) -> Sequence[bytes]:
        self.parent_queries += 1
        return self.history[revision]

    async def generations(self, revisions: Iterable[bytes]) -> Mapping[bytes, int]:
        return {r: self.generation[r] for r in revisions if r in self.generation}


def user() -> Principal:
    return Principal(
        id="usr_u",
        type=PrincipalType.USER,
        display_name="u",
        method="oidc",
        subjects=("usr_u",),
    )


def grant(store: Any, role: str = "viewer", scope: str = "agent/a") -> Authorizer:
    if scope == "*":
        store.add_grant("acme", "usr_u", role)
    else:
        store.add_grant("acme", "usr_u", role, GrantScope.BRANCH, scope)
    return Authorizer(AuthzSettings(), store=store)


async def read(
    authorizer: Authorizer,
    *,
    branch: str | None = None,
    at: str | None = None,
    permissions: Sequence[Permission] = READ,
    max_depth: int = 100,
    source: FakeSource | None = None,
    kind: str | None = None,
) -> ReadTarget:
    principal = user()
    context = AccessContext(kind=kind)
    wide = all([await authorizer.allows(principal, p, "acme", context) for p in permissions])
    return await authorize_read(
        source or FakeSource(),
        authorizer,
        principal,
        permissions,
        "acme",
        branch=branch,
        at=at,
        wide=wide,
        max_depth=max_depth,
        kind=kind,
    )


async def denied(authorizer: Authorizer, **kwargs: Any) -> ProblemError:
    with pytest.raises(ProblemError) as raised:
        await read(authorizer, **kwargs)
    assert raised.value.status_code == 403
    return raised.value


# Branch-narrowed grant ----------------------------------------------------------------------


async def test_narrowed_grant_reads_its_branch_at_head(store: Any) -> None:
    target = await read(grant(store), branch="agent/a")
    assert (target.revision, target.ref, target.kind) == (A2, "agent/a", RefKind.BRANCH)


async def test_narrowed_grant_reads_its_branch_named_in_at(store: Any) -> None:
    assert (await read(grant(store), at="agent/a")).revision == A2


async def test_narrowed_grant_reads_a_revision_of_its_branch(store: Any) -> None:
    subject = grant(store)
    assert (await read(subject, at=A1.hex())).revision == A1
    assert (await read(subject, at=A2.hex())).revision == A2
    assert (await read(subject, at=M1.hex())).revision == M1  # shared history is reachable
    assert (await read(subject, at="v-a1")).revision == A1


async def test_narrowed_grant_cannot_read_another_branch_through_at(store: Any) -> None:
    subject = grant(store)
    error = await denied(subject, branch="agent/a", at="main")
    assert error.extensions["permission"] == "schema.read"  # type: ignore[attr-defined]
    await denied(subject, at="agent/b")
    await denied(subject, branch="agent/a", at=M3.hex())
    await denied(subject, at=B1.hex())
    await denied(subject, at="v-m3")


async def test_narrowed_grant_cannot_read_the_default_branch_by_omission(store: Any) -> None:
    await denied(grant(store))  # no branch, no at: the default branch, which is `main`
    await denied(grant(store), branch="main")


async def test_narrowed_grant_cannot_read_an_unreachable_revision(store: Any) -> None:
    await denied(grant(store), at=X.hex())


async def test_narrowed_grant_gets_403_not_404_for_unknown_names(store: Any) -> None:
    subject = grant(store)
    await denied(subject, at=UNKNOWN.hex())
    await denied(subject, at="no-such-ref")
    await denied(subject, branch="no-such-branch")


async def test_pattern_grant_reaches_the_history_of_every_matching_branch(store: Any) -> None:
    subject = grant(store, scope="agent/*")
    assert (await read(subject, at=B1.hex())).revision == B1
    assert (await read(subject, at=A2.hex())).revision == A2
    await denied(subject, at=M2.hex())  # only on main, past the fork point


async def test_reachability_is_capped_by_depth(store: Any) -> None:
    subject = grant(store)
    await denied(subject, at=A1.hex(), max_depth=0)
    assert (await read(subject, at=A2.hex(), max_depth=0)).revision == A2
    await denied(subject, at=M1.hex(), max_depth=1)
    assert (await read(subject, at=M1.hex(), max_depth=2)).revision == M1


async def test_every_permission_must_cover_the_branch(store: Any) -> None:
    store.add_grant("acme", "usr_u", "viewer", GrantScope.BRANCH, "agent/a")
    store.add_grant("acme", "usr_u", "viewer", GrantScope.BRANCH, "agent/b")
    subject = Authorizer(AuthzSettings(), store=store)
    assert (await read(subject, at=B1.hex(), permissions=BOTH)).revision == B1
    await denied(subject, at=M3.hex(), permissions=BOTH)


async def test_kind_scoped_grant_reaches_no_revision(store: Any) -> None:
    store.add_grant("acme", "usr_u", "viewer", GrantScope.KIND, "Supplier")
    subject = Authorizer(AuthzSettings(), store=store)
    await denied(subject, at=A1.hex())
    await denied(subject, branch="agent/a")


async def test_kind_scoped_grant_reads_its_kind_at_any_revision(store: Any) -> None:
    store.add_grant("acme", "usr_u", "viewer", GrantScope.KIND, "Supplier")
    subject = Authorizer(AuthzSettings(), store=store)
    data = (Permission.DATA_READ,)
    assert (await read(subject, at=X.hex(), permissions=data, kind="Supplier")).revision == X
    assert (await read(subject, at="v-m3", permissions=data, kind="Supplier")).revision == M3
    await denied(subject, at=X.hex(), permissions=data, kind="Part")


# Domain-wide grant --------------------------------------------------------------------------


async def test_domain_wide_grant_reads_any_revision(store: Any) -> None:
    subject = grant(store, scope="*")
    assert (await read(subject, at="main")).revision == M3
    assert (await read(subject, at=B1.hex())).revision == B1
    assert (await read(subject, at=X.hex())).revision == X
    assert (await read(subject, at="v-m3")).revision == M3
    assert (await read(subject)).ref == "main"
    assert (await read(subject, branch="agent/a")).revision == A2


async def test_domain_wide_grant_still_gets_404_for_unknown_names(store: Any) -> None:
    subject = grant(store, scope="*")
    with pytest.raises(ProblemError) as raised:
        await read(subject, at=UNKNOWN.hex())
    assert raised.value.status_code == 404


async def test_no_grant_is_refused_before_any_lookup(store: Any) -> None:
    subject = Authorizer(AuthzSettings(), store=store)
    with pytest.raises(ProblemError) as raised:
        await resolve_readable(
            cast(StoreAccess, None),  # never reached: the check comes first
            subject,
            user(),
            READ,
            "acme",
            branch=None,
            at=A1.hex(),
            max_depth=10,
        )
    assert raised.value.status_code == 403


async def resolve_in_ghost(authorizer: Authorizer, principal: Principal) -> ProblemError:
    with pytest.raises(ProblemError) as raised:
        await resolve_readable(
            cast(StoreAccess, None),  # never reached: the check comes first
            authorizer,
            principal,
            READ,
            "ghost",  # not a Domain
            branch=None,
            at=None,
            max_depth=10,
        )
    return raised.value


async def test_missing_domain_is_404_for_installation_admins(store: Any) -> None:
    admin = Principal(
        id="usr_ops",
        type=PrincipalType.USER,
        display_name="ops",
        method="oidc",
        subjects=("usr_ops",),
        installation_admin=True,
    )
    error = await resolve_in_ghost(Authorizer(AuthzSettings(), store=store), admin)
    assert error.status_code == 404


async def test_missing_domain_is_404_for_a_domain_wide_grant(store: Any) -> None:
    # a Domain-wide role without schema.read: the permission check fails before any lookup
    store.add_grant("ghost", "usr_u", "data-only")
    settings = AuthzSettings(custom_roles={"data-only": ["data.read"]})
    error = await resolve_in_ghost(Authorizer(settings, store=store), user())
    assert error.status_code == 404


async def test_missing_domain_is_403_for_ungranted_callers(store: Any) -> None:
    error = await resolve_in_ghost(Authorizer(AuthzSettings(), store=store), user())
    assert error.status_code == 403
    assert error.extensions["permission"] == "schema.read"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("scope_type", "scope_ref"), [(GrantScope.BRANCH, "agent/a"), (GrantScope.KIND, "Supplier")]
)
async def test_missing_domain_is_403_for_narrowed_callers(
    store: Any, scope_type: GrantScope, scope_ref: str
) -> None:
    store.add_grant("ghost", "usr_u", "viewer", scope_type, scope_ref)
    authorizer = Authorizer(AuthzSettings(), store=store)
    guard = ReadGuard(authorizer, user(), READ, "ghost", 10)
    await guard.precheck()  # the caller holds the permission through a narrowed grant
    wide = await guard.wide()
    assert not wide
    error = guard.unknown(ProblemError(NOT_FOUND, "Domain 'ghost' not found."), wide=wide)
    assert error.status_code == 403


# Reachability -------------------------------------------------------------------------------


async def test_reachable_follows_every_parent_of_a_merge() -> None:
    source = FakeSource({**PARENTS, MERGE: (A2, B1)})
    source.generation[MERGE] = 4
    assert await reachable(source, [MERGE], B1, 5)
    assert await reachable(source, [MERGE], A1, 5)
    assert not await reachable(source, [MERGE], M2, 5)
    assert not await reachable(source, [MERGE], B1, 0)


async def test_reachable_does_not_walk_below_the_target() -> None:
    source = FakeSource()
    assert not await reachable(source, [A2], B1, 50)
    assert source.parent_queries == 1  # only A2: A1 is already at B1's generation


def test_reachability_depth_setting_has_a_default() -> None:
    assert ReadsSettings().reachability_max_depth == 1000
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        ReadsSettings(reachability_max_depth=0)


# Domain-wide reads (Source runs) -----------------------------------------------------------


def guard(authorizer: Authorizer) -> ReadGuard:
    return ReadGuard(authorizer, user(), (Permission.DATA_READ,), "acme", max_depth=100)


async def test_domain_wide_reads_need_a_grant_on_the_whole_domain(store: Any) -> None:
    await guard(grant(store, scope="*")).require_wide()


@pytest.mark.parametrize(
    ("scope_type", "scope_ref"), [(GrantScope.BRANCH, "main"), (GrantScope.KIND, "k_supplier")]
)
async def test_domain_wide_reads_refuse_narrowed_grants(
    store: Any, scope_type: GrantScope, scope_ref: str
) -> None:
    store.add_grant("acme", "usr_u", "viewer", scope_type, scope_ref)
    with pytest.raises(ProblemError) as raised:
        await guard(Authorizer(AuthzSettings(), store=store)).require_wide()
    assert raised.value.status_code == 403
    assert raised.value.extensions["permission"] == "data.read"  # type: ignore[attr-defined]
