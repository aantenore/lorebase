"""Portable Domain bundles: export a Domain to a directory or tarball, import it elsewhere.

A bundle holds the versioned content of one Domain: every Revision reachable from a ref with its
parents and changeset, the tree nodes and objects those Revisions reach, the refs and their
log, the provenance entries, and a readable `schemas/<branch>.yaml` per branch head. It does
not hold the audit log, principals, grants, proposals, documents, vault or embeddings (see
docs/operations.md). Materialized heads are not stored: import rebuilds them from the trees.

Layout (the `.jsonl` names carry `.gz` when `store.bundle.compression` is gzip)::

    manifest.json      format version, Lorebase version, counts, heads, sha256 per file
    domain.json        the Domain row
    refs.json          refs (kind, head, version, protection, materialization, materialized)
    ref_log.jsonl      head movements of every ref
    revisions.jsonl    id, root, generation, parents, author, message, origin, stats, created_at
    changesets.jsonl   element changes of each Revision
    provenance.jsonl   provenance entries
    nodes.jsonl        tree nodes: hash and base64 body
    objects.jsonl      objects: hash, type and canonical body
    schemas/<ref>.yaml schema at the head of each branch (informational)

Revisions are ordered by generation, so a parent always precedes its children. Everything is
written and read in batches: memory is bounded by `store.bundle.batch_rows` and by one tree
level while marking. Export reads one repeatable-read snapshot. Import verifies the sha256 of
every file, recomputes every Revision id, node hash and object hash, checks that every
reachable node and object is present, and runs in one transaction: a failing import leaves
nothing behind.

Revision ids include the Domain id, so a bundle can only be imported under the id it was
exported with.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import hashlib
import importlib.metadata
import itertools
import json
import logging
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from sqlalchemy import ARRAY, LargeBinary, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.canonical import ObjectType, from_hex, node_hash
from lorebase_core.canonical import hex as to_hex
from lorebase_core.model import dump_schema
from lorebase_core.revision import (
    Author,
    Origin,
    Revision,
    RevisionStats,
    format_timestamp,
    next_generation,
    revision_id,
)
from lorebase_core.tree import decode_node
from lorebase_store.bulk import OnConflict, bulk_write
from lorebase_store.gc import Marker
from lorebase_store.materialize import rebuild, verify
from lorebase_store.materialize.rebuild import load_schema
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.nodestore import NodeRepository
from lorebase_store.objectstore import OBJECT_TYPES_BY_CODE, PgObjectStore, encode_object
from lorebase_store.repositories.changesets import CHANGESETS
from lorebase_store.repositories.domains import DomainNotFoundError, DomainRepository, DomainRow
from lorebase_store.repositories.provenance import PROVENANCE
from lorebase_store.repositories.refs import REF_LOG, REFS, RefRepository
from lorebase_store.repositories.revisions import REVISION_PARENTS, REVISIONS, RevisionRepository
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime, default_runtime

log = logging.getLogger(__name__)

FORMAT = "lorebase-domain-bundle"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
SCHEMAS_DIR = "schemas"
TAR_SUFFIX = ".tar"

_JSONL = ("revisions", "changesets", "provenance", "ref_log", "nodes", "objects")
_PLAIN = ("domain.json", "refs.json")
_HASH_CHUNK = 1 << 20


class BundleError(Exception):
    """The bundle cannot be written, is malformed, or fails verification."""


class DomainNotEmptyError(BundleError):
    """The target Domain already has Revisions or refs."""


@dataclass(slots=True)
class BundleReport:
    """What an export or import moved."""

    domain_id: str
    path: str
    revisions: int = 0
    changesets: int = 0
    provenance: int = 0
    nodes: int = 0
    objects: int = 0
    refs: int = 0
    ref_log: int = 0
    schemas: int = 0
    rebuilt_branches: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------------
# Files


def _jsonl_name(base: str, compression: str) -> str:
    return f"{base}.jsonl.gz" if compression == "gzip" else f"{base}.jsonl"


def _dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class _Output:
    """The files of a bundle being written into a directory."""

    def __init__(self, root: Path, compression: str, level: int) -> None:
        self.root = root
        self.compression = compression
        self.level = level
        self.files: dict[str, dict[str, Any]] = {}

    @contextmanager
    def jsonl(self, base: str) -> Iterator[_LineWriter]:
        name = _jsonl_name(base, self.compression)
        path = self.root / name
        raw = path.open("wb")
        stream: _Sink = raw
        if self.compression == "gzip":
            stream = gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, compresslevel=self.level, mtime=0
            )
        writer = _LineWriter(stream)
        try:
            yield writer
        finally:
            stream.close()
            raw.close()
        self._register(name, path, writer.records)

    def plain(self, name: str, content: bytes, records: int | None = None) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self._register(name, path, records)

    def _register(self, name: str, path: Path, records: int | None) -> None:
        digest, size = _sha256(path)
        entry: dict[str, Any] = {"sha256": digest, "bytes": size}
        if records is not None:
            entry["records"] = records
        self.files[name] = entry


class _Sink(Protocol):
    def write(self, data: bytes, /) -> Any: ...

    def close(self) -> None: ...


class _LineWriter:
    def __init__(self, stream: _Sink) -> None:
        self._stream = stream
        self.records = 0

    def write(self, value: Any) -> None:
        self._stream.write(_dumps(value) + b"\n")
        self.records += 1


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rb") as handle:
        for number, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except ValueError as error:
                raise BundleError(f"{path.name} line {number}: not JSON") from error
            if not isinstance(value, dict):
                raise BundleError(f"{path.name} line {number}: not an object")
            yield value


def _batches[T](items: Iterable[T], size: int) -> Iterator[tuple[T, ...]]:
    iterator = iter(items)
    while batch := tuple(itertools.islice(iterator, size)):
        yield batch


# --------------------------------------------------------------------------------------
# Export


def _bytes(name: str) -> Any:
    return bindparam(name, type_=ARRAY(LargeBinary()))


_REVISIONS = text(
    """
    SELECT r.id, r.root, r.generation, r.author, r.on_behalf_of, r.message, r.origin, r.stats,
           r.created_at
    FROM revisions r JOIN lb_gc_revs m ON m.id = r.id
    WHERE r.domain_id = :d AND (r.generation, r.id) > (:g, :i)
    ORDER BY r.generation, r.id LIMIT :n
    """
)
_PARENTS = text(
    "SELECT revision_id, parent_id FROM revision_parents "
    "WHERE domain_id = :d AND revision_id = ANY(CAST(:ids AS bytea[])) "
    "ORDER BY revision_id, position"
).bindparams(_bytes("ids"))
_CHANGESETS = text(
    """
    SELECT c.revision_id, c.partition, c.element_key, c.old_hash, c.new_hash, c.provenance
    FROM changesets c JOIN lb_gc_revs m ON m.id = c.revision_id
    WHERE c.domain_id = :d AND (c.revision_id, c.partition, c.element_key) > (:r, :p, :k)
    ORDER BY c.revision_id, c.partition, c.element_key LIMIT :n
    """
)
_PROVENANCE = text(
    "SELECT id, origin_type, origin_ref, actor, at FROM provenance "
    "WHERE domain_id = :d AND id > :last ORDER BY id LIMIT :n"
)
_REF_LOG = text(
    "SELECT ref_name, seq, old_head, new_head, reason, actor, at FROM ref_log "
    "WHERE domain_id = :d AND (ref_name, seq) > (:r, :s) ORDER BY ref_name, seq LIMIT :n"
)
_NODES = text(
    "SELECT n.hash, n.body FROM tree_nodes n JOIN lb_gc_nodes m ON m.hash = n.hash "
    "WHERE n.domain_id = :d AND n.hash > :last ORDER BY n.hash LIMIT :n"
)
_OBJECTS = text(
    "SELECT o.hash, o.object_type, o.body FROM objects o JOIN lb_gc_objects m ON m.hash = o.hash "
    "WHERE o.domain_id = :d AND o.hash > :last ORDER BY o.hash LIMIT :n"
)
_REFS = text(
    """
    SELECT r.name, r.kind::text AS kind, r.head, r.protected, r.materialization::text AS
           materialization, r.version, r.created_at, r.created_by,
           EXISTS (SELECT 1 FROM mat_schema s WHERE s.domain_id = r.domain_id
                   AND s.branch = r.name) AS materialized
    FROM refs r WHERE r.domain_id = :d ORDER BY r.name
    """
)


def _hex(value: bytes | None) -> str | None:
    return None if value is None else to_hex(bytes(value))


def _timestamp(value: datetime) -> str:
    return format_timestamp(value)


async def export_domain(
    engine: AsyncEngine,
    domain_id: str,
    destination: str | Path,
    *,
    runtime: StoreRuntime | None = None,
) -> BundleReport:
    """Write a bundle of `domain_id` to `destination`: a new directory, or a `.tar` file.

    Reads one consistent snapshot; commits made meanwhile are not part of the bundle.
    """
    runtime = runtime or default_runtime()
    target = Path(destination)
    started = time.monotonic()
    as_tar = target.suffix == TAR_SUFFIX
    _check_destination(target, as_tar)
    if as_tar:
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".bundle-") as work:
            report = await _export(engine, domain_id, Path(work), runtime, str(target))
            _pack(Path(work), target)
    else:
        report = await _export(engine, domain_id, target, runtime, str(target))
    report.seconds = time.monotonic() - started
    return report


def _check_destination(target: Path, as_tar: bool) -> None:
    if as_tar:
        if target.exists():
            raise BundleError(f"{target} exists")
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise BundleError(f"{target} exists and is not an empty directory")
        target.mkdir(parents=True, exist_ok=True)


def _pack(work: Path, target: Path) -> None:
    partial = target.with_name(target.name + ".partial")
    with tarfile.open(partial, "w") as archive:
        for path in sorted(work.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(work).as_posix())
    partial.rename(target)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


async def _export(
    engine: AsyncEngine, domain_id: str, root: Path, runtime: StoreRuntime, label: str
) -> BundleReport:
    settings = runtime.settings
    batch = settings.bundle.batch_rows
    out = _Output(root, settings.bundle.compression, settings.bundle.compression_level)
    report = BundleReport(domain_id, label)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        await scope_to_domain(session, domain_id)
        domain = await DomainRepository(session).get(domain_id)
        if domain is None:
            raise DomainNotFoundError(domain_id)
        refs = [dict(row) for row in (await session.execute(_REFS, {"d": domain_id})).mappings()]
        marker = Marker(session, domain_id, batch=settings.gc.mark_batch)
        await marker.prepare()
        await marker.mark(attempts=False)
        marked = await marker.stats()
        if marked.missing_nodes:
            raise BundleError(f"{marked.missing_nodes} reachable tree nodes are missing")

        out.plain("domain.json", _dumps(_domain_json(domain)))
        out.plain("refs.json", _dumps([_ref_json(ref) for ref in refs]), len(refs))
        report.refs = len(refs)
        report.revisions = await _export_revisions(session, out, domain_id, batch)
        report.changesets = await _export_changesets(session, out, domain_id, batch)
        report.provenance = await _export_provenance(session, out, domain_id, batch)
        report.ref_log = await _export_ref_log(session, out, domain_id, batch)
        report.nodes = await _export_nodes(session, out, domain_id, batch)
        report.objects = await _export_objects(session, out, domain_id, batch)
        for name, got, want in (
            ("revisions", report.revisions, marked.revisions),
            ("nodes", report.nodes, marked.nodes),
            ("objects", report.objects, marked.objects),
        ):
            if got != want:
                raise BundleError(f"expected {want} {name} but the store holds {got}")
        report.schemas = await _export_schemas(session, out, domain_id, refs, runtime)
        await marker.drop()

    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "lorebase_version": _lorebase_version(),
        "domain_id": domain_id,
        "created_at": format_timestamp(datetime.now(UTC)),
        "compression": out.compression,
        "heads": {ref["name"]: to_hex(bytes(ref["head"])) for ref in refs},
        "counts": {
            "revisions": report.revisions,
            "changesets": report.changesets,
            "provenance": report.provenance,
            "ref_log": report.ref_log,
            "nodes": report.nodes,
            "objects": report.objects,
            "refs": report.refs,
        },
        "files": out.files,
    }
    (root / MANIFEST).write_bytes(json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n")
    return report


def _lorebase_version() -> str:
    try:
        return importlib.metadata.version("lorebase-store")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _domain_json(domain: DomainRow) -> dict[str, Any]:
    return {
        "id": domain.id,
        "display_name": domain.display_name,
        "description": domain.description,
        "default_branch": domain.default_branch,
        "leaf_size": domain.leaf_size,
        "settings": domain.settings,
        "created_at": _timestamp(domain.created_at),
        "created_by": domain.created_by,
    }


def _ref_json(ref: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": ref["name"],
        "kind": ref["kind"],
        "head": to_hex(bytes(ref["head"])),
        "protected": ref["protected"],
        "materialization": ref["materialization"],
        "version": ref["version"],
        "created_at": _timestamp(ref["created_at"]),
        "created_by": ref["created_by"],
        "materialized": ref["materialized"],
    }


async def _export_revisions(session: AsyncSession, out: _Output, domain: str, batch: int) -> int:
    after: tuple[int, bytes] = (0, b"")
    with out.jsonl("revisions") as lines:
        while True:
            rows = (
                await session.execute(
                    _REVISIONS, {"d": domain, "g": after[0], "i": after[1], "n": batch}
                )
            ).all()
            if not rows:
                return lines.records
            ids = [bytes(row.id) for row in rows]
            parents: dict[bytes, list[str]] = {}
            for revision, parent in await session.execute(_PARENTS, {"d": domain, "ids": ids}):
                parents.setdefault(bytes(revision), []).append(to_hex(bytes(parent)))
            for row in rows:
                lines.write(
                    {
                        "id": to_hex(bytes(row.id)),
                        "root": to_hex(bytes(row.root)),
                        "generation": row.generation,
                        "parents": parents.get(bytes(row.id), []),
                        "author": row.author,
                        "on_behalf_of": row.on_behalf_of,
                        "message": row.message,
                        "origin": row.origin,
                        "stats": row.stats,
                        "created_at": _timestamp(row.created_at),
                    }
                )
            after = (int(rows[-1].generation), bytes(rows[-1].id))


async def _export_changesets(session: AsyncSession, out: _Output, domain: str, batch: int) -> int:
    after: tuple[bytes, str, str] = (b"", "", "")
    with out.jsonl("changesets") as lines:
        while True:
            rows = (
                await session.execute(
                    _CHANGESETS,
                    {"d": domain, "r": after[0], "p": after[1], "k": after[2], "n": batch},
                )
            ).all()
            if not rows:
                return lines.records
            for row in rows:
                lines.write(
                    {
                        "revision": to_hex(bytes(row.revision_id)),
                        "partition": row.partition,
                        "key": row.element_key,
                        "old": _hex(row.old_hash),
                        "new": _hex(row.new_hash),
                        "provenance": row.provenance,
                    }
                )
            last = rows[-1]
            after = (bytes(last.revision_id), last.partition, last.element_key)


async def _export_provenance(session: AsyncSession, out: _Output, domain: str, batch: int) -> int:
    last = ""
    with out.jsonl("provenance") as lines:
        while True:
            rows = (
                await session.execute(_PROVENANCE, {"d": domain, "last": last, "n": batch})
            ).all()
            if not rows:
                return lines.records
            for row in rows:
                lines.write(
                    {
                        "id": row.id,
                        "origin_type": row.origin_type,
                        "origin_ref": row.origin_ref,
                        "actor": row.actor,
                        "at": _timestamp(row.at),
                    }
                )
            last = rows[-1].id


async def _export_ref_log(session: AsyncSession, out: _Output, domain: str, batch: int) -> int:
    after: tuple[str, int] = ("", 0)
    with out.jsonl("ref_log") as lines:
        while True:
            rows = (
                await session.execute(
                    _REF_LOG, {"d": domain, "r": after[0], "s": after[1], "n": batch}
                )
            ).all()
            if not rows:
                return lines.records
            for row in rows:
                lines.write(
                    {
                        "ref": row.ref_name,
                        "seq": row.seq,
                        "old": _hex(row.old_head),
                        "new": to_hex(bytes(row.new_head)),
                        "reason": row.reason,
                        "actor": row.actor,
                        "at": _timestamp(row.at),
                    }
                )
            after = (rows[-1].ref_name, int(rows[-1].seq))


async def _export_nodes(session: AsyncSession, out: _Output, domain: str, batch: int) -> int:
    last = b""
    with out.jsonl("nodes") as lines:
        while True:
            rows = (await session.execute(_NODES, {"d": domain, "last": last, "n": batch})).all()
            if not rows:
                return lines.records
            for row in rows:
                lines.write(
                    {
                        "hash": to_hex(bytes(row.hash)),
                        "body": base64.b64encode(bytes(row.body)).decode("ascii"),
                    }
                )
            last = bytes(rows[-1].hash)


async def _export_objects(session: AsyncSession, out: _Output, domain: str, batch: int) -> int:
    last = b""
    with out.jsonl("objects") as lines:
        while True:
            rows = (await session.execute(_OBJECTS, {"d": domain, "last": last, "n": batch})).all()
            if not rows:
                return lines.records
            for row in rows:
                lines.write(
                    {
                        "hash": to_hex(bytes(row.hash)),
                        "type": OBJECT_TYPES_BY_CODE[row.object_type].value,
                        "body": row.body,
                    }
                )
            last = bytes(rows[-1].hash)


async def _export_schemas(
    session: AsyncSession,
    out: _Output,
    domain_id: str,
    refs: Sequence[Mapping[str, Any]],
    runtime: StoreRuntime,
) -> int:
    settings = runtime.settings
    nodes = NodeRepository(
        session, domain_id, writes=settings.writes, batch=settings.nodes.prefetch_batch
    )
    revisions = RevisionRepository(session, domain_id)
    count = 0
    for ref in refs:
        if ref["kind"] != RefKind.BRANCH.value:
            continue
        head = await revisions.require(bytes(ref["head"]))
        schema, _ = await load_schema(
            session, domain_id, await nodes.load_tree(head.root), runtime=runtime
        )
        name = f"{SCHEMAS_DIR}/{quote(ref['name'], safe='')}.yaml"
        out.plain(name, dump_schema(schema).encode())
        count += 1
    return count


# --------------------------------------------------------------------------------------
# Import


def _extract(tarball: Path, into: Path) -> None:
    """Unpack a bundle tarball, accepting only plain files with the bundle's names."""
    with tarfile.open(tarball, "r") as archive:
        members = archive.getmembers()
        for member in members:
            parts = Path(member.name).parts
            if (
                not member.isfile()
                or member.name.startswith("/")
                or ".." in parts
                or len(parts) > 2
                or (len(parts) == 2 and parts[0] != SCHEMAS_DIR)
            ):
                raise BundleError(f"unexpected entry {member.name!r} in {tarball.name}")
        archive.extractall(into, members=members, filter="data")


@contextmanager
def _opened(source: Path) -> Iterator[Path]:
    if source.is_dir():
        yield source
    elif source.is_file() and source.suffix == TAR_SUFFIX:
        with tempfile.TemporaryDirectory(prefix="lorebase-bundle-") as work:
            _extract(source, Path(work))
            yield Path(work)
    else:
        raise BundleError(f"{source} is neither a bundle directory nor a {TAR_SUFFIX} file")


def read_manifest(root: Path) -> dict[str, Any]:
    """The manifest of an opened bundle directory, checked for format and version."""
    path = root / MANIFEST
    if not path.is_file():
        raise BundleError(f"{MANIFEST} not found")
    try:
        manifest = json.loads(path.read_bytes())
    except ValueError as error:
        raise BundleError(f"{MANIFEST} is not valid JSON") from error
    if manifest.get("format") != FORMAT:
        raise BundleError(f"not a Lorebase Domain bundle (format {manifest.get('format')!r})")
    version = manifest.get("format_version")
    if version != FORMAT_VERSION:
        raise BundleError(
            f"bundle format version {version!r} is not supported (this release reads "
            f"{FORMAT_VERSION})"
        )
    for key in ("domain_id", "compression", "counts", "files", "heads"):
        if key not in manifest:
            raise BundleError(f"{MANIFEST} lacks {key!r}")
    return manifest  # type: ignore[no-any-return]


def _verify_files(root: Path, manifest: Mapping[str, Any]) -> None:
    files: Mapping[str, Mapping[str, Any]] = manifest["files"]
    present = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST
    }
    expected = set(files)
    if present != expected:
        raise BundleError(
            f"files differ from the manifest: missing {sorted(expected - present)}, "
            f"unexpected {sorted(present - expected)}"
        )
    for name, entry in files.items():
        digest, size = _sha256(root / name)
        if digest != entry["sha256"] or size != entry["bytes"]:
            raise BundleError(f"{name} does not match its sha256 in the manifest")


@dataclass(slots=True)
class _Import:
    session: AsyncSession
    root: Path
    manifest: Mapping[str, Any]
    domain_id: str
    runtime: StoreRuntime
    batch: int
    report: BundleReport

    def lines(self, base: str) -> Iterator[dict[str, Any]]:
        return _read_jsonl(self.root / _jsonl_name(base, self.manifest["compression"]))

    def expect(self, name: str, got: int) -> None:
        want = self.manifest["counts"][name]
        if got != want:
            raise BundleError(f"{name}: manifest says {want}, bundle holds {got}")


async def import_bundle(
    engine: AsyncEngine,
    source: str | Path,
    *,
    domain_id: str | None = None,
    runtime: StoreRuntime | None = None,
    verify_heads: bool | None = None,
) -> BundleReport:
    """Import a bundle (directory or `.tar`) into `domain_id`, which must not exist yet or be
    empty (no Revisions, no refs). It defaults to, and must equal, the bundle's Domain id.

    Every file hash, Revision id, node hash and object hash is verified, materialized heads are
    rebuilt for the refs that had one, and with `verify_heads` (default
    `store.bundle.verify_after_import`) each rebuilt branch is checked against its tree. All
    in one transaction. Raises `BundleError` (the transaction is rolled back).
    """
    runtime = runtime or default_runtime()
    started = time.monotonic()
    path = Path(source)
    with _opened(path) as root:
        manifest = read_manifest(root)
        target = domain_id or manifest["domain_id"]
        if target != manifest["domain_id"]:
            raise BundleError(
                f"the bundle is of Domain {manifest['domain_id']!r}; Revision ids include the "
                f"Domain id, so it cannot be imported as {target!r}"
            )
        _verify_files(root, manifest)
        settings = runtime.settings
        report = BundleReport(target, str(path))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session, session.begin():
            await scope_to_domain(session, target)
            context = _Import(
                session, root, manifest, target, runtime, settings.bundle.batch_rows, report
            )
            await _prepare_domain(context)
            await _import_objects(context)
            await _import_nodes(context)
            await _import_revisions(context)
            await _import_changesets(context)
            await _import_provenance(context)
            refs = await _import_refs(context)
            await _import_ref_log(context)
            await _check_integrity(context)
            await _materialize(
                context,
                refs,
                settings.bundle.verify_after_import if verify_heads is None else verify_heads,
            )
    report.seconds = time.monotonic() - started
    return report


async def _prepare_domain(c: _Import) -> None:
    domains = DomainRepository(c.session)
    data = _read_json(c.root / "domain.json")
    if data["id"] != c.domain_id:
        raise BundleError("domain.json names a different Domain than the manifest")
    if await domains.get(c.domain_id) is not None:
        counts = (
            await c.session.execute(
                text(
                    "SELECT (SELECT count(*) FROM revisions WHERE domain_id = :d), "
                    "(SELECT count(*) FROM refs WHERE domain_id = :d)"
                ),
                {"d": c.domain_id},
            )
        ).one()
        if counts[0] or counts[1]:
            raise DomainNotEmptyError(
                f"Domain {c.domain_id!r} already has {counts[0]} Revisions and {counts[1]} refs"
            )
        return
    await domains.create(
        DomainRow(
            id=data["id"],
            display_name=data["display_name"],
            description=data["description"],
            default_branch=data["default_branch"],
            leaf_size=data["leaf_size"],
            settings=data["settings"],
            created_at=datetime.fromisoformat(data["created_at"]),
            created_by=data["created_by"],
        )
    )


async def _import_objects(c: _Import) -> None:
    store = PgObjectStore(c.session, c.domain_id, writes=c.runtime.settings.writes, batch=c.batch)
    total = 0
    for batch in _batches(c.lines("objects"), c.batch):
        encoded = []
        for line in batch:
            try:
                item = encode_object(ObjectType(line["type"]), line["body"])
            except (ValueError, KeyError, TypeError) as error:
                raise BundleError(f"object {line.get('hash')}: {error}") from error
            if to_hex(item.hash) != line["hash"]:
                raise BundleError(f"object {line['hash']} does not match its content")
            encoded.append(item)
        await store.put_many(encoded)
        total += len(batch)
    c.report.objects = total
    c.expect("objects", total)


async def _import_nodes(c: _Import) -> None:
    repository = NodeRepository(
        c.session,
        c.domain_id,
        writes=c.runtime.settings.writes,
        batch=c.runtime.settings.nodes.prefetch_batch,
    )
    total = 0
    for batch in _batches(c.lines("nodes"), c.batch):
        nodes: dict[bytes, bytes] = {}
        for line in batch:
            body = base64.b64decode(line["body"], validate=True)
            digest = from_hex(line["hash"])
            if node_hash(body) != digest:
                raise BundleError(f"tree node {line['hash']} does not match its content")
            decode_node(body)  # rejects malformed nodes
            nodes[digest] = body
        await repository.put_many(nodes)
        total += len(batch)
    c.report.nodes = total
    c.expect("nodes", total)


async def _import_revisions(c: _Import) -> None:
    revisions = RevisionRepository(c.session, c.domain_id)
    writes = c.runtime.settings.writes
    total = 0
    previous = 0
    for batch in _batches(c.lines("revisions"), c.batch):
        local: dict[bytes, int] = {}
        parsed: list[tuple[dict[str, Any], Revision, bytes]] = []
        for line in batch:
            revision = _revision(c.domain_id, line)
            digest = revision_id(revision)
            if to_hex(digest) != line["id"]:
                raise BundleError(f"Revision {line['id']} does not match its content")
            if line["generation"] < previous:
                raise BundleError("revisions.jsonl is not ordered by generation")
            previous = line["generation"]
            parsed.append((line, revision, digest))
        outside = {p for _, rev, _ in parsed for p in rev.parents} - {d for _, _, d in parsed}
        known = await revisions.generations(outside)
        rows: list[dict[str, Any]] = []
        parent_rows: list[dict[str, Any]] = []
        for line, revision, digest in parsed:
            try:
                generation = next_generation(local.get(p) or known[p] for p in revision.parents)
            except KeyError as error:
                raise BundleError(
                    f"Revision {line['id']} comes before its parent {to_hex(error.args[0])}"
                ) from error
            if generation != line["generation"]:
                raise BundleError(f"Revision {line['id']} has the wrong generation")
            local[digest] = generation
            rows.append(
                {
                    "domain_id": c.domain_id,
                    "id": digest,
                    "root": revision.root,
                    "generation": generation,
                    "author": revision.author.principal,
                    "on_behalf_of": revision.author.on_behalf_of,
                    "message": revision.message,
                    "origin": revision.origin.model_dump(),
                    "stats": revision.stats.model_dump(),
                    "created_at": revision.created_at,
                }
            )
            parent_rows.extend(
                {
                    "domain_id": c.domain_id,
                    "revision_id": digest,
                    "position": position,
                    "parent_id": parent,
                }
                for position, parent in enumerate(revision.parents)
            )
        await bulk_write(c.session, REVISIONS, rows, writes=writes, on_conflict=OnConflict.ERROR)
        await bulk_write(
            c.session, REVISION_PARENTS, parent_rows, writes=writes, on_conflict=OnConflict.ERROR
        )
        total += len(batch)
    c.report.revisions = total
    c.expect("revisions", total)


def _revision(domain_id: str, line: Mapping[str, Any]) -> Revision:
    try:
        return Revision(
            parents=tuple(from_hex(p) for p in line["parents"]),
            root=from_hex(line["root"]),
            domain=domain_id,
            author=Author(principal=line["author"], on_behalf_of=line["on_behalf_of"]),
            message=line["message"],
            origin=Origin(**line["origin"]),
            created_at=datetime.fromisoformat(line["created_at"]),
            stats=RevisionStats(**line["stats"]),
        )
    except (ValueError, KeyError, TypeError) as error:
        raise BundleError(f"Revision {line.get('id')}: {error}") from error


def _from_hex(value: str | None) -> bytes | None:
    return None if value is None else from_hex(value)


async def _import_changesets(c: _Import) -> None:
    writes = c.runtime.settings.writes
    total = 0
    for batch in _batches(c.lines("changesets"), c.batch):
        rows = [
            {
                "domain_id": c.domain_id,
                "revision_id": from_hex(line["revision"]),
                "partition": line["partition"],
                "element_key": line["key"],
                "old_hash": _from_hex(line["old"]),
                "new_hash": _from_hex(line["new"]),
                "provenance": line["provenance"] or None,
            }
            for line in batch
        ]
        await bulk_write(c.session, CHANGESETS, rows, writes=writes, on_conflict=OnConflict.ERROR)
        total += len(rows)
    c.report.changesets = total
    c.expect("changesets", total)


async def _import_provenance(c: _Import) -> None:
    writes = c.runtime.settings.writes
    total = 0
    for batch in _batches(c.lines("provenance"), c.batch):
        rows = [
            {
                "domain_id": c.domain_id,
                "id": line["id"],
                "origin_type": line["origin_type"],
                "origin_ref": line["origin_ref"],
                "actor": line["actor"],
                "at": datetime.fromisoformat(line["at"]),
            }
            for line in batch
        ]
        await bulk_write(c.session, PROVENANCE, rows, writes=writes, on_conflict=OnConflict.ERROR)
        total += len(rows)
    c.report.provenance = total
    c.expect("provenance", total)


async def _import_refs(c: _Import) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = _read_json(c.root / "refs.json")
    heads = c.manifest["heads"]
    rows = []
    for ref in refs:
        if heads.get(ref["name"]) != ref["head"]:
            raise BundleError(f"ref {ref['name']!r} disagrees with the manifest heads")
        rows.append(
            {
                "domain_id": c.domain_id,
                "name": ref["name"],
                "kind": RefKind(ref["kind"]),
                "head": from_hex(ref["head"]),
                "protected": ref["protected"],
                "materialization": Materialization(ref["materialization"]),
                "version": ref["version"],
                "created_at": datetime.fromisoformat(ref["created_at"]),
                "created_by": ref["created_by"],
            }
        )
    await bulk_write(
        c.session, REFS, rows, writes=c.runtime.settings.writes, on_conflict=OnConflict.ERROR
    )
    c.report.refs = len(rows)
    c.expect("refs", len(rows))
    return refs


async def _import_ref_log(c: _Import) -> None:
    writes = c.runtime.settings.writes
    total = 0
    for batch in _batches(c.lines("ref_log"), c.batch):
        rows = [
            {
                "domain_id": c.domain_id,
                "ref_name": line["ref"],
                "seq": line["seq"],
                "old_head": _from_hex(line["old"]),
                "new_head": from_hex(line["new"]),
                "reason": line["reason"],
                "actor": line["actor"],
                "at": datetime.fromisoformat(line["at"]),
            }
            for line in batch
        ]
        await bulk_write(c.session, REF_LOG, rows, writes=writes, on_conflict=OnConflict.ERROR)
        total += len(rows)
    c.report.ref_log = total
    c.expect("ref_log", total)


async def _check_integrity(c: _Import) -> None:
    """Every Revision is reachable, every reachable node and object is stored."""
    marker = Marker(c.session, c.domain_id, batch=c.runtime.settings.gc.mark_batch)
    await marker.prepare()
    await marker.mark(attempts=False)
    stats = await marker.stats()
    missing_objects = (
        await c.session.execute(
            text(
                "SELECT count(*) FROM lb_gc_objects m WHERE NOT EXISTS "
                "(SELECT 1 FROM objects o WHERE o.domain_id = :d AND o.hash = m.hash)"
            ),
            {"d": c.domain_id},
        )
    ).scalar_one()
    await marker.drop()
    if stats.missing_nodes or missing_objects:
        raise BundleError(
            f"the bundle lacks {stats.missing_nodes} tree nodes and {missing_objects} objects "
            "that its Revisions reach"
        )
    if stats.revisions != c.report.revisions:
        raise BundleError(
            f"{c.report.revisions - stats.revisions} Revisions are not reachable from any ref"
        )


async def _materialize(c: _Import, refs: Sequence[Mapping[str, Any]], check: bool) -> None:
    for ref in refs:
        if ref["kind"] != RefKind.BRANCH.value or not ref["materialized"]:
            continue
        await rebuild(c.session, c.domain_id, ref["name"], runtime=c.runtime)
        c.report.rebuilt_branches.append(ref["name"])
        if check:
            result = await verify(c.session, c.domain_id, ref["name"], runtime=c.runtime)
            if not result.ok:
                raise BundleError(
                    f"branch {ref['name']!r} fails verification after rebuild: "
                    f"{result.divergences[:3]}"
                )
    heads = RefRepository(c.session, c.domain_id)
    for ref in refs:  # the refs must still be exactly the bundle's
        stored = await heads.require(ref["name"])
        if stored.head != from_hex(ref["head"]) or stored.version != ref["version"]:
            raise BundleError(f"ref {ref['name']!r} changed while importing")
    schemas = len([name for name in c.manifest["files"] if name.startswith(SCHEMAS_DIR + "/")])
    c.report.schemas = schemas


# --------------------------------------------------------------------------------------
# Command line


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lorebase_store.bundle",
        description="Export and import portable Domain bundles. Reads the database and "
        "store.bundle settings from the Lorebase configuration.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Write a bundle of a Domain.")
    export.add_argument("--domain", required=True, help="Domain id.")
    export.add_argument(
        "--out", required=True, type=Path, help="New directory, or a path ending in .tar."
    )
    load = commands.add_parser("import", help="Import a bundle into an empty Domain.")
    load.add_argument("--in", dest="source", required=True, type=Path, help="Directory or .tar.")
    load.add_argument("--domain", default=None, help="Target Domain id (must equal the bundle's).")
    load.add_argument(
        "--no-verify", action="store_true", help="Skip verifying rebuilt branches against trees."
    )
    return parser


async def _main(args: argparse.Namespace) -> int:
    from lorebase_platform.settings import load_settings
    from lorebase_store.db import create_engine
    from lorebase_store.settings import database_settings, store_settings

    settings = load_settings()
    runtime = StoreRuntime.from_settings(
        store_settings(settings), database=database_settings(settings)
    )
    engine = create_engine(database_settings(settings), pooled=False, statement_timeout_ms=0)
    try:
        if args.command == "export":
            report = await export_domain(engine, args.domain, args.out, runtime=runtime)
        else:
            report = await import_bundle(
                engine,
                args.source,
                domain_id=args.domain,
                runtime=runtime,
                verify_heads=False if args.no_verify else None,
            )
    except (BundleError, DomainNotFoundError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    finally:
        await engine.dispose()
    sys.stdout.write(json.dumps(report.as_dict()) + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
