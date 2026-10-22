"""PDF and DOCX parsing in a limited child process (`lorebase_scribe.extraction.sandbox`)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from lorebase_scribe.extraction.parsing import (
    DOCX,
    MARKDOWN,
    PDF,
    ParsedDocument,
    ParseError,
    parse_document,
)
from lorebase_scribe.extraction.sandbox import parse_isolated
from lorebase_scribe.extraction.settings import ParserSettings

HERE = str(Path(__file__).resolve().parent)
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="resource limits are POSIX only")


def limits(**overrides: object) -> ParserSettings:
    return ParserSettings.model_validate({"timeout_s": 30, **overrides})


async def run(target: str, settings: ParserSettings, *, max_chars: int = 1000) -> ParsedDocument:
    return await parse_isolated(
        b"%PDF-1.4 fake",
        PDF,
        settings,
        max_chars=max_chars,
        target=f"sandbox_targets:{target}",
        python_path=[HERE],
    )


async def test_real_documents_parse_as_in_the_worker() -> None:
    pypdf = pytest.importorskip("pypdf")
    docx = pytest.importorskip("docx")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    pdf = io.BytesIO()
    writer.write(pdf)
    document = docx.Document()
    document.add_heading("Agreement C-3025", level=1)
    document.add_paragraph("The agreement starts on 8 February 2024.")
    word = io.BytesIO()
    document.save(word)
    for data, media_type in ((pdf.getvalue(), PDF), (word.getvalue(), DOCX)):
        isolated = await parse_isolated(data, media_type, limits(), max_chars=1000)
        assert isolated == parse_document(data, media_type)


async def test_parser_errors_come_back_as_parse_errors() -> None:
    pytest.importorskip("pypdf")
    with pytest.raises(ParseError, match="unreadable PDF"):
        await parse_isolated(b"%PDF-1.4 broken", PDF, limits(), max_chars=1000)


async def test_a_slow_document_is_stopped_at_the_wall_clock_limit() -> None:
    with pytest.raises(ParseError, match=r"timeout_s \(1.0 s\)"):
        await run("sleeps", limits(timeout_s=1))


@POSIX
async def test_a_document_that_spins_is_stopped_at_the_cpu_limit() -> None:
    with pytest.raises(ParseError, match="cpu_s"):
        await run("spins", limits(cpu_s=1))


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced on Linux")
async def test_a_document_that_allocates_is_stopped_at_the_memory_limit() -> None:
    with pytest.raises(ParseError, match="memory"):
        await run("allocates", limits(memory_mb=1024))


async def test_huge_text_is_cut_one_character_past_the_limit() -> None:
    parsed = await run("huge_text", limits(), max_chars=1000)
    assert parsed.text == "x" * 1001


async def test_documents_above_the_size_limit_are_not_parsed() -> None:
    with pytest.raises(ParseError, match="max_input_bytes"):
        await parse_isolated(b"x" * 11, MARKDOWN, limits(max_input_bytes=10), max_chars=100)


async def test_the_parser_has_no_network_and_no_proxy_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    parsed = await run("connects", limits())
    assert parsed.text == "refused refused refused"


@POSIX
async def test_a_killed_parser_is_a_parse_error() -> None:
    with pytest.raises(ParseError, match="signal 9"):
        await run("dies", limits())


async def test_markdown_is_decoded_in_the_worker() -> None:
    parsed = await parse_isolated(b"# Title\n", MARKDOWN, limits(), max_chars=100)
    assert parsed.text == "# Title\n"
