from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest

from lorebase_guide.__main__ import main
from lorebase_guide.modes import FileModeMemory, mode_memory_path
from lorebase_guide.settings import GuideSettings

AGE = 3600.0
DIGEST = bytes(range(32))
OTHER = bytes(32)


async def test_a_new_memory_recalls_what_an_earlier_one_remembered(tmp_path: Path) -> None:
    path = tmp_path / "state" / "modes.json"
    await FileModeMemory(path).remember(DIGEST, 1, AGE)

    restarted = FileModeMemory(path)
    assert await restarted.recall(DIGEST, AGE) == 1
    assert await restarted.recall(OTHER, AGE) is None
    stored = json.loads(path.read_text())
    assert stored[DIGEST.hex()]["index"] == 1
    assert set(stored) == {DIGEST.hex()}
    # only the lock file sits next to the data file
    assert {p.name for p in path.parent.iterdir()} == {"modes.json", "modes.json.lock"}


async def test_modes_only_move_towards_weaker_ones(tmp_path: Path) -> None:
    memory = FileModeMemory(tmp_path / "modes.json")
    await memory.remember(DIGEST, 2, AGE)
    await memory.remember(DIGEST, 1, AGE)
    await memory.remember(OTHER, 1, AGE)
    assert await memory.recall(DIGEST, AGE) == 2
    assert await memory.recall(OTHER, AGE) == 1


@pytest.mark.parametrize(
    "content",
    ["{not json", "[1, 2]", '{"ab": "x", "cd": true}', f'{{"{DIGEST.hex()}": 2}}'],
)
async def test_a_damaged_file_is_ignored_and_rewritten(tmp_path: Path, content: str) -> None:
    path = tmp_path / "modes.json"
    path.write_text(content)
    memory = FileModeMemory(path)
    assert await memory.recall(DIGEST, AGE) is None
    await memory.remember(DIGEST, 1, AGE)
    assert await memory.recall(DIGEST, AGE) == 1


async def test_io_errors_never_raise(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        memory = FileModeMemory(locked / "sub" / "modes.json")
        await memory.remember(DIGEST, 1, AGE)
        assert await memory.recall(DIGEST, AGE) is None
        unreadable = FileModeMemory(tmp_path)  # a directory: reading fails
        assert await unreadable.recall(DIGEST, AGE) is None
    finally:
        locked.chmod(stat.S_IRWXU)


def test_the_path_comes_from_the_settings(tmp_path: Path) -> None:
    defaults = GuideSettings()
    default = mode_memory_path(defaults.data_dir, defaults.mode_memory_file)
    assert default is not None
    assert default == Path("~/.local/state/lorebase/guide").expanduser() / (
        "structured_output_modes.json"
    )
    assert mode_memory_path(str(tmp_path), "m.json") == tmp_path / "m.json"
    assert mode_memory_path(str(tmp_path), str(tmp_path / "abs.json")) == tmp_path / "abs.json"
    assert mode_memory_path(str(tmp_path), None) is None


async def test_entries_expire_and_are_replaced(tmp_path: Path) -> None:
    now = [1000.0]
    memory = FileModeMemory(tmp_path / "modes.json", clock=lambda: now[0])
    await memory.remember(DIGEST, 2, AGE)
    now[0] += AGE - 1
    assert await memory.recall(DIGEST, AGE) == 2
    now[0] += 2
    assert await memory.recall(DIGEST, AGE) is None
    await memory.remember(DIGEST, 1, AGE)  # the expired, stronger entry does not win
    assert await memory.recall(DIGEST, AGE) == 1


async def test_clear_forgets_everything(tmp_path: Path) -> None:
    memory = FileModeMemory(tmp_path / "modes.json")
    await memory.remember(DIGEST, 1, AGE)
    await memory.remember(OTHER, 2, AGE)
    assert memory.clear() == 2
    assert await memory.recall(DIGEST, AGE) is None
    assert FileModeMemory(tmp_path / "none" / "modes.json").clear() == 0


def test_the_command_clears_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "modes.json"
    asyncio.run(FileModeMemory(path).remember(DIGEST, 1, AGE))
    monkeypatch.setenv("LOREBASE__GUIDE__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOREBASE__GUIDE__MODE_MEMORY_FILE", "modes.json")
    main(["modes", "clear"])
    assert not path.exists()


async def test_a_write_prunes_expired_entries_and_the_lock_file_is_private(tmp_path: Path) -> None:
    now = [1000.0]
    path = tmp_path / "modes.json"
    memory = FileModeMemory(path, clock=lambda: now[0])
    await memory.remember(OTHER, 2, AGE)
    now[0] += AGE + 1
    await memory.remember(DIGEST, 1, AGE)
    assert set(json.loads(path.read_text())) == {DIGEST.hex()}
    if fcntl_available():
        assert stat.S_IMODE((tmp_path / "modes.json.lock").stat().st_mode) == 0o600


def fcntl_available() -> bool:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return False
    return True
