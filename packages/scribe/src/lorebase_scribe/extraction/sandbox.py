"""PDF and DOCX parsing in a limited child process (`scribe.extraction.parser`).

pypdf and python-docx run on bytes uploaded by Domain editors; a crafted file can make them loop
or allocate without bound. `parse_isolated` therefore hands the bytes to a child Python process
(`python -I -B -m lorebase_scribe.extraction.sandbox`) over standard input and reads a JSON
answer from standard output. The child, before it reads anything:

- lowers its own limits: CPU seconds (`RLIMIT_CPU`), address space (`RLIMIT_AS`, where the
  system enforces it) and core files (none); `resource` exists on POSIX only, elsewhere only
  the wall-clock limit applies;
- replaces the socket constructors and name resolution with functions that refuse, so a parser
  cannot open a connection, and runs with an empty environment (no proxy variables);
- cuts the text at the character limit it is given, so the answer stays bounded.

The parent bounds the whole exchange by `timeout_s` and kills the child when it expires, reads
at most a fixed number of bytes from it, and turns every failure (limit, crash, garbled answer)
into a `ParseError`, which fails the extraction job without a retry.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import os
import signal
import socket
import sys
from collections.abc import Callable, Sequence
from typing import Any, NoReturn

from lorebase_scribe.extraction.parsing import (
    MARKDOWN,
    PLAIN_TEXT,
    ParsedDocument,
    ParseError,
    parse_document,
)
from lorebase_scribe.extraction.settings import ParserSettings

DEFAULT_TARGET = "lorebase_scribe.extraction.parsing:parse_document"
"""The function the child runs: `(data: bytes, media_type: str) -> ParsedDocument`."""
NATIVE = (MARKDOWN, PLAIN_TEXT)
"""Media types decoded in the worker: no third-party parser is involved."""
ANSWER_OVERHEAD_BYTES = 16 * 1024 * 1024
"""Room in the child's answer beyond the text: page offsets, warnings, JSON escapes."""
_READ = 64 * 1024


def _answer_limit(max_chars: int) -> int:
    # JSON escapes a character into at most 12 bytes (a surrogate pair as two \\uXXXX).
    return max_chars * 12 + ANSWER_OVERHEAD_BYTES


async def parse_isolated(
    data: bytes,
    media_type: str,
    settings: ParserSettings,
    *,
    max_chars: int,
    target: str = DEFAULT_TARGET,
    python_path: Sequence[str] = (),
) -> ParsedDocument:
    """Text of a document, parsed under `settings`; raises `ParseError`.

    `max_chars` bounds the text returned (one more character is kept, so the caller still sees
    that the text was longer). `target` and `python_path` choose the function the child runs.
    """
    if len(data) > settings.max_input_bytes:
        raise ParseError(
            f"the document has {len(data)} bytes, more than "
            f"scribe.extraction.parser.max_input_bytes ({settings.max_input_bytes})"
        )
    if media_type in NATIVE or not settings.isolated:
        return await asyncio.to_thread(parse_document, data, media_type)
    command = [
        sys.executable,
        "-I",
        "-B",
        "-m",
        __name__,
        "--media-type",
        media_type,
        "--cpu-s",
        str(settings.cpu_s),
        "--memory-bytes",
        str(settings.memory_mb * 1024 * 1024),
        "--max-chars",
        str(max_chars + 1),
        "--target",
        target,
    ]
    for entry in python_path:
        command += ["--path", entry]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_child_environment(),
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(settings.timeout_s):
            answer = await _exchange(process, data, _answer_limit(max_chars + 1))
    except TimeoutError:
        raise ParseError(
            f"parsing took longer than scribe.extraction.parser.timeout_s ({settings.timeout_s} s)"
        ) from None
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    return _decode_answer(answer, process.returncode, settings)


def _child_environment() -> dict[str, str]:
    """Nothing from the worker's environment (no secrets, no proxies) but what Python needs."""
    return {name: os.environ[name] for name in ("SYSTEMROOT",) if name in os.environ}


async def _exchange(process: asyncio.subprocess.Process, data: bytes, limit: int) -> bytes:
    stdin, stdout = process.stdin, process.stdout
    assert stdin is not None
    assert stdout is not None

    async def feed() -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.write(data)
            await stdin.drain()
            stdin.close()

    feeding = asyncio.create_task(feed())
    answer = bytearray()
    try:
        while block := await stdout.read(_READ):
            answer.extend(block)
            if len(answer) > limit:
                raise ParseError("the parser answered more than its output limit")
        await feeding
    finally:
        feeding.cancel()
    await process.wait()
    return bytes(answer)


def _decode_answer(
    answer: bytes, returncode: int | None, settings: ParserSettings
) -> ParsedDocument:
    if returncode is not None and returncode < 0:
        stopped = -returncode
        if stopped == getattr(signal, "SIGXCPU", None):
            raise ParseError(
                f"parsing used more than scribe.extraction.parser.cpu_s ({settings.cpu_s} s) "
                "of CPU time"
            )
        raise ParseError(
            f"the parser process was stopped by signal {stopped} (memory limit "
            f"scribe.extraction.parser.memory_mb is {settings.memory_mb} MiB)"
        )
    try:
        found = json.loads(answer)
    except ValueError:
        raise ParseError(f"the parser process failed (exit status {returncode})") from None
    if not isinstance(found, dict):
        raise ParseError("the parser process answered something other than a document")
    if "error" in found:
        raise ParseError(str(found["error"]))
    try:
        return ParsedDocument(
            text=str(found["text"]),
            media_type=str(found["media_type"]),
            pages=tuple(int(p) for p in found.get("pages", ())),
            warnings=tuple(str(w) for w in found.get("warnings", ())),
        )
    except (KeyError, TypeError, ValueError):
        raise ParseError("the parser process answered an incomplete document") from None


# The child --------------------------------------------------------------------------------------


def _refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise OSError("network access is disabled while parsing documents")


def _deny_network() -> None:
    class _NoSocket(socket.socket):
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            _refuse()

    socket.socket = _NoSocket  # type: ignore[misc]
    for name in ("create_connection", "create_server", "getaddrinfo", "socketpair", "fromfd"):
        setattr(socket, name, _refuse)


def _limit(cpu_s: int, memory_bytes: int) -> None:
    try:
        import resource
    except ImportError:  # not POSIX: the parent's wall-clock limit still applies
        return
    limits = (
        (resource.RLIMIT_CPU, cpu_s, cpu_s + 1),
        (resource.RLIMIT_AS, memory_bytes, memory_bytes),
        (resource.RLIMIT_CORE, 0, 0),
    )
    for which, soft, hard in limits:
        _, current_hard = resource.getrlimit(which)
        if current_hard != resource.RLIM_INFINITY:
            soft, hard = min(soft, current_hard), min(hard, current_hard)
        # Some systems (macOS for RLIMIT_AS) refuse or ignore a limit; the others still hold.
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(which, (soft, hard))


def _function(target: str) -> Callable[[bytes, str], ParsedDocument]:
    module, _, name = target.partition(":")
    function: Callable[[bytes, str], ParsedDocument] = getattr(
        importlib.import_module(module), name
    )
    return function


def _write(answer: dict[str, Any]) -> None:
    sys.stdout.buffer.write(json.dumps(answer).encode("utf-8"))
    sys.stdout.buffer.flush()


def main(argv: Sequence[str] | None = None) -> int:
    """Child entry point: parse the bytes of standard input, answer JSON on standard output."""
    parser = argparse.ArgumentParser(prog="lorebase_scribe.extraction.sandbox")
    parser.add_argument("--media-type", required=True)
    parser.add_argument("--cpu-s", type=int, required=True)
    parser.add_argument("--memory-bytes", type=int, required=True)
    parser.add_argument("--max-chars", type=int, required=True)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--path", action="append", default=[])
    args = parser.parse_args(argv)
    _limit(args.cpu_s, args.memory_bytes)
    _deny_network()
    sys.path[:0] = args.path
    data = sys.stdin.buffer.read()
    try:
        parsed = _function(args.target)(data, args.media_type)
    except ParseError as error:
        _write({"error": str(error)})
        return 0
    except MemoryError:
        _write({"error": "parsing ran out of memory (scribe.extraction.parser.memory_mb)"})
        return 0
    except Exception as error:  # any parser failure is one of the document
        _write({"error": f"the parser failed on this document ({type(error).__name__})"})
        return 0
    _write(
        {
            "text": parsed.text[: args.max_chars],
            "media_type": parsed.media_type,
            "pages": list(parsed.pages),
            "warnings": list(parsed.warnings),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
