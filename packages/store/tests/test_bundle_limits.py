"""Reading bundles within the store.bundle limits: sizes, entries, lines, links (no database)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from lorebase_store.bundle import (
    FORMAT,
    FORMAT_VERSION,
    MANIFEST,
    BundleError,
    _opened,
    _read_jsonl,
    _verify_files,
    import_bundle,
    read_manifest,
)
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import BundleSettings, StoreSettings

# Every failure below happens before the database is touched.
NO_DATABASE = cast(AsyncEngine, object())

FILES = {
    "domain.json": b'{"id": "supply"}',
    "refs.json": b"[]",
    "objects.jsonl": b'{"hash": "00"}\n{"hash": "01"}\n',
    "schemas/main.yaml": b"entities: {}\n",
}


def manifest_for(files: dict[str, bytes]) -> bytes:
    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "domain_id": "supply",
        "compression": "none",
        "counts": {},
        "heads": {},
        "files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in files.items()
        },
    }
    return json.dumps(manifest).encode()


def directory(root: Path, files: dict[str, bytes] | None = None) -> Path:
    content = {**FILES, **(files or {})}
    for name, data in {**content, MANIFEST: manifest_for(content)}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def tarball(path: Path, files: dict[str, bytes] | None = None, *extra: tarfile.TarInfo) -> Path:
    content = {**FILES, **(files or {})}
    with tarfile.open(path, "w") as archive:
        for name, data in {**content, MANIFEST: manifest_for(content)}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info in extra:
            archive.addfile(info, io.BytesIO(b"x" * info.size) if info.isreg() else None)
    return path


def runtime(**limits: int) -> StoreRuntime:
    return StoreRuntime.from_settings(StoreSettings(bundle=BundleSettings(**limits)))


async def refused(source: Path, match: str, **limits: int) -> None:
    with pytest.raises(BundleError, match=match):
        await import_bundle(NO_DATABASE, source, runtime=runtime(**limits))


def test_well_formed_bundles_pass_the_checks(tmp_path: Path) -> None:
    limits = BundleSettings()
    for source in (directory(tmp_path / "dir"), tarball(tmp_path / "b.tar")):
        with _opened(source, limits) as root:
            _verify_files(root, read_manifest(root, limits), limits)


async def test_oversize_member_is_refused(tmp_path: Path) -> None:
    big = {"objects.jsonl": b'{"hash": "00"}\n' * 100}
    await refused(
        directory(tmp_path / "dir", big),
        r"objects\.jsonl.*max_member_bytes \(1000\)",
        max_member_bytes=1000,
    )
    await refused(
        tarball(tmp_path / "b.tar", big),
        r"objects\.jsonl.*max_member_bytes \(1000\)",
        max_member_bytes=1000,
    )


async def test_oversize_total_is_refused(tmp_path: Path) -> None:
    await refused(directory(tmp_path / "dir"), r"max_total_bytes \(40\)", max_total_bytes=40)
    await refused(tarball(tmp_path / "b.tar"), r"max_total_bytes \(40\)", max_total_bytes=40)


async def test_too_many_entries_are_refused(tmp_path: Path) -> None:
    await refused(directory(tmp_path / "dir"), r"max_entries \(3\)", max_entries=3)
    await refused(tarball(tmp_path / "b.tar"), r"max_entries \(3\)", max_entries=3)


def test_overlong_line_is_refused(tmp_path: Path) -> None:
    root = directory(tmp_path, {"objects.jsonl": b'{"hash": "' + b"0" * 200 + b'"}\n'})
    limits = BundleSettings(max_line_bytes=100)
    with pytest.raises(BundleError, match=r"objects\.jsonl line 1 .*max_line_bytes \(100\)"):
        list(_read_jsonl(root, "objects.jsonl", limits))
    # A line exactly at the limit, newline aside, is read.
    exact = b'{"a": "' + b"0" * 91 + b'"}'
    assert len(exact) == 100
    (root / "exact.jsonl").write_bytes(exact + b"\n")
    assert list(_read_jsonl(root, "exact.jsonl", limits)) == [{"a": "0" * 91}]


def test_decompressed_size_is_counted(tmp_path: Path) -> None:
    (tmp_path / "objects.jsonl.gz").write_bytes(gzip.compress(b'{"hash": "00"}\n' * 1000))
    limits = BundleSettings(max_member_bytes=1000)
    with pytest.raises(BundleError, match=r"objects\.jsonl\.gz .*max_member_bytes \(1000\)"):
        list(_read_jsonl(tmp_path, "objects.jsonl.gz", limits))


def member(
    name: str, kind: bytes = tarfile.REGTYPE, size: int = 0, link: str = ""
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size
    info.linkname = link
    return info


async def test_tarball_symlink_member_is_refused(tmp_path: Path) -> None:
    archive = tarball(
        tmp_path / "b.tar", None, member("schemas/x.yaml", tarfile.SYMTYPE, link="/etc/passwd")
    )
    await refused(archive, r"'schemas/x\.yaml' in b\.tar is a symbolic link")


async def test_tarball_hardlink_member_is_refused(tmp_path: Path) -> None:
    archive = tarball(
        tmp_path / "b.tar", None, member("schemas/x.yaml", tarfile.LNKTYPE, link="refs.json")
    )
    await refused(archive, r"'schemas/x\.yaml' in b\.tar is a hard link")


async def test_tarball_special_member_is_refused(tmp_path: Path) -> None:
    archive = tarball(tmp_path / "b.tar", None, member("schemas/x.yaml", tarfile.FIFOTYPE))
    await refused(archive, "is not a regular file or directory")


async def test_tarball_absolute_path_is_refused(tmp_path: Path) -> None:
    archive = tarball(tmp_path / "b.tar", None, member("/tmp/x.yaml", size=1))
    await refused(archive, "has an absolute path")


async def test_tarball_parent_path_is_refused(tmp_path: Path) -> None:
    archive = tarball(tmp_path / "b.tar", None, member("schemas/../../x.yaml", size=1))
    await refused(archive, r"leaves the bundle directory")


async def test_tarball_header_size_is_checked_before_reading(tmp_path: Path) -> None:
    archive = tarball(tmp_path / "b.tar", None, member("schemas/big.yaml", size=5000))
    await refused(archive, r"'schemas/big\.yaml' in b\.tar is larger than", max_member_bytes=4096)


async def test_directory_symlink_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(b"secret: 1\n")
    root = directory(tmp_path / "dir", {"schemas/x.yaml": outside.read_bytes()})
    (root / "schemas" / "x.yaml").unlink()
    (root / "schemas" / "x.yaml").symlink_to(outside)
    await refused(root, r"schemas/x\.yaml is a symbolic link")


async def test_directory_symlinked_folder_is_refused(tmp_path: Path) -> None:
    root = directory(tmp_path / "dir")
    elsewhere = tmp_path / "elsewhere"
    (root / "schemas").rename(elsewhere)
    (root / "schemas").symlink_to(elsewhere, target_is_directory=True)
    await refused(root, "schemas is a symbolic link")


async def test_directory_special_file_is_refused(tmp_path: Path) -> None:
    root = directory(tmp_path / "dir")
    os.mkfifo(root / "schemas" / "pipe.yaml")
    await refused(root, r"schemas/pipe\.yaml is not a regular file or directory")
