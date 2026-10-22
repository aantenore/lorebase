"""Three-way merge engine (design 6.8 to 6.10): orchestration and resolution application.

``merge`` finds the merge base (a virtual one for criss-cross histories, AC-13), merges the
schema, then the data under the merged schema, then checks consistency. The outcome is a
``MergedTree`` ready for the commit pipeline, or a ``MergeAttempt`` listing Conflicts.
``apply_resolutions`` re-runs the merge with the resolver's decisions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from lorebase_core.merge.conflicts import (
    AutoResolution,
    Conflict,
    ConflictKind,
    Resolution,
    ResolutionChoice,
    ResolutionError,
    Side,
    UnresolvedConflictsError,
)
from lorebase_core.merge.consistency import check_consistency
from lorebase_core.merge.data_merge import DataMerge, SideChanges, merge_data, side_changes
from lorebase_core.merge.index import TreeIndexView
from lorebase_core.merge.objects import (
    ObjectStore,
    SchemaLoader,
    data_object_type,
    tree_schema_loader,
)
from lorebase_core.merge.policies import ChangeStamp, MergePolicy
from lorebase_core.merge.resolve import emit_groups
from lorebase_core.merge.schema_merge import SchemaMerge, known_static_issues, merge_schema
from lorebase_core.merge.state import SIDES, Context, Directives, check_choice
from lorebase_core.revision import (
    ChangesetEntry,
    MergeStatus,
    RevisionGraph,
    RevisionStats,
    changeset_between,
    changeset_stats,
    merge_base,
)
from lorebase_core.tree import (
    PARTITION_SCHEMA,
    NodeStore,
    TreeChange,
    TreeParams,
    tree_apply,
    tree_diff,
    tree_entries,
)
from lorebase_core.validate import IndexView, SchemaRules

__all__ = [
    "IndexViewFactory",
    "LineageLoader",
    "MergeAttempt",
    "MergeInput",
    "MergeOutcome",
    "MergeResult",
    "MergedTree",
    "ProvenanceLoader",
    "apply_resolutions",
    "merge",
]

IndexViewFactory = Callable[[bytes], IndexView]
"""Returns the ``IndexView`` of the target head, given the target head Revision id."""

ProvenanceLoader = Callable[[bytes, str, str], Mapping[str, str]]
"""``(revision id, partition, key) -> {attribute id: provenance id}`` of an element."""

LineageLoader = Callable[[bytes, str, str], Mapping[str, ChangeStamp]]
"""``(revision id, partition, key) -> {attribute id: change stamp}``: the provenance stamp of
the last change of each Attribute of an element at a Revision. Read by the ``authority``
and ``newest_wins`` policies for ``ours`` and ``theirs``; an element without Lineage maps to
an empty mapping."""

_VIRTUAL_LABEL = b"lb1\x00virtual-base\x00"


@dataclass(frozen=True)
class MergeInput:
    """Everything a merge reads. ``theirs`` (source) is merged into ``ours`` (target).

    ``base`` overrides the merge base search. Without ``schema_loader`` schemas are read from
    the ``schema`` partition; without ``index_view`` the target head is read from its tree.
    ``resolutions`` are matched to Conflicts by fingerprint. ``schema_rules`` are the options
    of the static checks of the merged schema (the defaults when unset). ``lineage`` gives the
    ``authority`` and ``newest_wins`` policies the provenance entries of both sides; without
    it those policies never decide.
    """

    store: NodeStore
    objects: ObjectStore
    graph: RevisionGraph
    revision_root: Callable[[bytes], bytes]
    ours: bytes
    theirs: bytes
    base: bytes | None = None
    schema_loader: SchemaLoader | None = None
    index_view: IndexViewFactory | None = None
    provenance: ProvenanceLoader | None = None
    lineage: LineageLoader | None = None
    policy: MergePolicy = field(default_factory=MergePolicy)
    tree_params: TreeParams = field(default_factory=TreeParams)
    resolutions: tuple[Resolution, ...] = ()
    schema_rules: SchemaRules | None = None


class MergeResult(StrEnum):
    UP_TO_DATE = "up_to_date"
    """Theirs is already contained in ours: the target does not change."""
    FAST_FORWARD = "fast_forward"
    """The target moves to theirs."""
    MERGED = "merged"
    """A merge Revision with parents ``(ours, theirs)`` is to be written."""


@dataclass(frozen=True)
class MergedTree:
    """A clean merge: the new tree and its changeset against ours (the first parent)."""

    result: MergeResult
    root: bytes
    ours: bytes
    theirs: bytes
    base: bytes | None
    virtual_base: bool
    parents: tuple[bytes, ...]
    changes: tuple[ChangesetEntry, ...]
    stats: RevisionStats
    auto_resolutions: tuple[AutoResolution, ...] = ()
    resolutions: tuple[Resolution, ...] = ()


@dataclass(frozen=True)
class MergeAttempt:
    """A merge with Conflicts, kept until every Conflict is resolved."""

    base: bytes | None
    ours: bytes
    theirs: bytes
    virtual_base: bool
    conflicts: tuple[Conflict, ...]
    auto_resolutions: tuple[AutoResolution, ...] = ()
    resolutions: tuple[Resolution, ...] = ()
    input: MergeInput | None = field(default=None, repr=False, compare=False)
    #: Full member keys of grouped Conflicts, by fingerprint.
    members: Mapping[str, tuple[str, ...]] = field(default_factory=dict, repr=False)


MergeOutcome = MergedTree | MergeAttempt


def merge(merge_input: MergeInput) -> MergeOutcome:
    """Merge ``theirs`` into ``ours`` (design 6.8 steps 0 to 5)."""
    return _Engine(merge_input).run()


def apply_resolutions(attempt: MergeAttempt, resolutions: Iterable[Resolution]) -> MergeOutcome:
    """Apply resolutions to an attempt and merge again.

    Every Conflict of the attempt needs a resolution with an accepted choice; grouped
    Conflicts resolved with per-member values need a value for every member. The merge is
    recomputed, so resolutions can surface new Conflicts.
    """
    if attempt.input is None:
        raise ResolutionError("the merge attempt does not carry its merge input")
    conflicts = {c.fingerprint: c for c in attempt.conflicts}
    given: dict[str, Resolution] = {}
    for resolution in resolutions:
        conflict = conflicts.get(resolution.fingerprint)
        if conflict is None:
            raise ResolutionError(f"no conflict with fingerprint {resolution.fingerprint}")
        check_choice(conflict, resolution)
        _check_members(attempt, conflict, resolution)
        given[resolution.fingerprint] = resolution
    unresolved = conflicts.keys() - given.keys()
    if unresolved:
        raise UnresolvedConflictsError(unresolved)
    combined = {r.fingerprint: r for r in attempt.resolutions}
    combined.update(given)
    return merge(replace(attempt.input, resolutions=tuple(combined.values())))


def _check_members(attempt: MergeAttempt, conflict: Conflict, resolution: Resolution) -> None:
    if resolution.choice is not ResolutionChoice.CUSTOM or resolution.values is None:
        return
    if conflict.kind in (ConflictKind.UNIQUENESS, ConflictKind.REQUIRED_RELATION):
        # A new value for some of the Records, or Links for some of them, can be enough.
        return
    members = attempt.members.get(conflict.fingerprint, ())
    missing = [m for m in members if m not in resolution.values]
    if missing and resolution.value is None:
        raise ResolutionError(
            f"custom values of {conflict.fingerprint} miss {len(missing)} member(s), "
            f"for example {missing[0]!r}"
        )


@dataclass
class _Run:
    ctx: Context
    schema: SchemaMerge
    data: DataMerge


class _Engine:
    def __init__(self, merge_input: MergeInput) -> None:
        self.input = merge_input
        self.store = merge_input.store
        self.loader = merge_input.schema_loader or tree_schema_loader(
            merge_input.store, merge_input.objects
        )
        self.virtual_roots: dict[bytes, bytes] = {}
        self.lineage_cache: dict[tuple[Side, str, str], Mapping[str, ChangeStamp]] = {}

    def root_of(self, revision: bytes | None) -> bytes | None:
        if revision is None:
            return None
        found = self.virtual_roots.get(revision)
        return found if found is not None else self.input.revision_root(revision)

    # Orchestration -------------------------------------------------------------------

    def run(self) -> MergeOutcome:
        merge_input = self.input
        ours, theirs = merge_input.ours, merge_input.theirs
        if merge_input.base is not None:
            base: bytes | None = merge_input.base
            status = (
                MergeStatus.UP_TO_DATE
                if base == theirs
                else MergeStatus.FAST_FORWARD
                if base == ours
                else MergeStatus.MERGE
            )
            virtual = False
        else:
            found = merge_base(merge_input.graph, ours, theirs, self.build_virtual)
            base, status, virtual = found.base, found.status, found.virtual
        ours_root = self.root_of(ours)
        theirs_root = self.root_of(theirs)
        assert ours_root is not None
        assert theirs_root is not None
        if status is MergeStatus.UP_TO_DATE:
            return self.unchanged(ours_root, base)
        if status is MergeStatus.FAST_FORWARD and merge_input.policy.fast_forward:
            return self.fast_forward(ours_root, theirs_root, base)
        return self.three_way(base, virtual)

    def unchanged(self, root: bytes, base: bytes | None) -> MergedTree:
        merge_input = self.input
        return MergedTree(
            result=MergeResult.UP_TO_DATE,
            root=root,
            ours=merge_input.ours,
            theirs=merge_input.theirs,
            base=base,
            virtual_base=False,
            parents=(),
            changes=(),
            stats=changeset_stats(()),
        )

    def fast_forward(self, ours_root: bytes, theirs_root: bytes, base: bytes | None) -> MergedTree:
        merge_input = self.input
        changes = tuple(
            replace(entry, provenance=self.provenance(merge_input.theirs, entry))
            for entry in changeset_between(self.store, ours_root, theirs_root)
        )
        return MergedTree(
            result=MergeResult.FAST_FORWARD,
            root=theirs_root,
            ours=merge_input.ours,
            theirs=merge_input.theirs,
            base=base,
            virtual_base=False,
            parents=(),
            changes=changes,
            stats=changeset_stats(changes),
        )

    def three_way(self, base: bytes | None, virtual: bool) -> MergeOutcome:
        merge_input = self.input
        roots = {
            Side.BASE: self.root_of(base),
            Side.OURS: self.root_of(merge_input.ours),
            Side.THEIRS: self.root_of(merge_input.theirs),
        }
        resolutions = {r.fingerprint: r for r in merge_input.resolutions}
        factory = merge_input.index_view
        index: IndexView = (
            factory(merge_input.ours)
            if factory is not None
            else TreeIndexView(self.store, merge_input.objects, roots[Side.OURS])
        )
        directives = Directives()
        known = None
        # Each round applies the directives of newly matched resolutions; a resolution
        # yields its directives once, so the rounds are bounded by their number.
        for _ in range(len(resolutions) + 1):
            current = self.execute(roots, resolutions, directives, index=index, known=known)
            known = current.ctx.known_issues
            if not directives.absorb(current.ctx.out):
                break
        ctx = current.ctx
        auto = tuple(ctx.auto)
        if ctx.conflicts:
            return MergeAttempt(
                base=base,
                ours=merge_input.ours,
                theirs=merge_input.theirs,
                virtual_base=virtual,
                conflicts=tuple(ctx.conflicts),
                auto_resolutions=auto,
                resolutions=merge_input.resolutions,
                input=merge_input,
                members=dict(ctx.members),
            )
        root, entries = self.write(current)
        return MergedTree(
            result=MergeResult.MERGED,
            root=root,
            ours=merge_input.ours,
            theirs=merge_input.theirs,
            base=base,
            virtual_base=virtual,
            parents=(merge_input.ours, merge_input.theirs),
            changes=entries,
            stats=changeset_stats(entries),
            auto_resolutions=auto,
            resolutions=merge_input.resolutions,
        )

    def execute(
        self,
        roots: dict[Side, bytes | None],
        resolutions: Mapping[str, Resolution],
        directives: Directives,
        *,
        index: IndexView | None,
        known: set[tuple[object, tuple[object, ...]]] | None = None,
    ) -> _Run:
        """One run of steps 1 to 4 under the given directives."""
        schemas = {
            side: None if root is None else self.loader(root) for side, root in roots.items()
        }
        ctx = Context(
            store=self.store,
            objects=self.input.objects,
            policy=self.input.policy,
            params=self.input.tree_params,
            schema_loader=self.loader,
            roots=roots,
            schemas=schemas,
            resolutions=resolutions,
            directives=directives.copy(),
            virtual=index is None,
            rules=self.input.schema_rules or SchemaRules(),
            lineage=None if self.input.lineage is None else self.lineage_of,
        )
        ctx.schema_hashes = {side: _schema_hashes(self.store, root) for side, root in roots.items()}
        if index is not None:
            ctx.known_issues = (
                known
                if known is not None
                else known_static_issues([schemas[Side.OURS], schemas[Side.THEIRS]], ctx.rules)
            )
        changes: dict[Side, SideChanges] = {
            side: side_changes(tree_diff(self.store, roots[Side.BASE], roots[side]))
            for side in SIDES
        }
        schema = merge_schema(ctx)
        data = merge_data(ctx, schema, changes)
        if index is not None:
            check_consistency(ctx, schema.schema, data, index)
            emit_groups(ctx, schema)
        return _Run(ctx, schema, data)

    def write(
        self, run: _Run, *, with_provenance: bool = True
    ) -> tuple[bytes, tuple[ChangesetEntry, ...]]:
        """Write the merged tree; return its root and its changeset against ours."""
        ctx, schema, data = run.ctx, run.schema, run.data
        theirs = self.input.theirs if with_provenance else None
        entries: list[ChangesetEntry] = []
        ours_schema = ctx.schema_hashes[Side.OURS]
        for key in sorted(ours_schema.keys() | schema.hashes.keys()):
            old, new = ours_schema.get(key), schema.hashes.get(key)
            if old != new:
                whole = schema.origins.get(key)
                lineage = (
                    self.provenance_of(theirs, PARTITION_SCHEMA, key)
                    if whole is Side.THEIRS
                    else {}
                )
                entries.append(ChangesetEntry(PARTITION_SCHEMA, key, old, new, lineage))
        tree_changes: list[TreeChange] = []
        for location in sorted(data.states):
            state = data.states[location]
            new = None if state is None else ctx.put(data_object_type(location[0]), state)
            old = data.ours.get(location)
            if old == new:
                continue
            tree_changes.append(TreeChange(location[0], location[1], new))
            origin = data.origins.get(location)
            provenance: Mapping[str, str] = {}
            if origin is not None and (origin.whole is Side.THEIRS or origin.attributes):
                full = self.provenance_of(theirs, *location)
                if origin.whole is Side.THEIRS:
                    provenance = full
                else:
                    provenance = {
                        a: p for a, p in full.items() if origin.attributes.get(a) is Side.THEIRS
                    }
            entries.append(ChangesetEntry(location[0], location[1], old, new, provenance))
        root = tree_apply(self.store, schema.root, tree_changes, self.input.tree_params)
        entries.sort(key=lambda e: (e.partition.encode("utf-8"), e.key))
        return root, tuple(entries)

    # Provenance -----------------------------------------------------------------------

    def provenance_of(self, revision: bytes | None, partition: str, key: str) -> Mapping[str, str]:
        loader = self.input.provenance
        if loader is None or revision is None:
            return {}
        return dict(loader(revision, partition, key))

    def provenance(self, revision: bytes, entry: ChangesetEntry) -> Mapping[str, str]:
        return self.provenance_of(revision, entry.partition, entry.key)

    def lineage_of(self, side: Side, partition: str, key: str) -> Mapping[str, ChangeStamp]:
        """Lineage entries of an element on ``ours`` or ``theirs``, loaded once per merge."""
        loader = self.input.lineage
        if loader is None or side is Side.BASE:
            return {}
        found = self.lineage_cache.get((side, partition, key))
        if found is None:
            revision = self.input.ours if side is Side.OURS else self.input.theirs
            found = self.lineage_cache[(side, partition, key)] = dict(
                loader(revision, partition, key)
            )
        return found

    # Virtual merge base (AC-13) -----------------------------------------------------------

    def build_virtual(self, base: bytes | None, ours: bytes, theirs: bytes) -> bytes:
        """Merge two common ancestors into a virtual base Revision (never committed).

        Conflicting changes keep the value of their own merge base, so that both sides of
        the outer merge see them as changes.
        """
        roots = {
            Side.BASE: self.root_of(base),
            Side.OURS: self.root_of(ours),
            Side.THEIRS: self.root_of(theirs),
        }
        run = self.execute(roots, {}, Directives(), index=None)
        root, _ = self.write(run, with_provenance=False)
        digest = hashlib.sha256(_VIRTUAL_LABEL)
        for part in (base or b"", ours, theirs, root):
            digest.update(part)
        virtual = digest.digest()
        self.virtual_roots[virtual] = root
        return virtual


def _schema_hashes(store: NodeStore, root: bytes | None) -> dict[str, bytes]:
    return dict(tree_entries(store, root, PARTITION_SCHEMA))
