"""Parsing, chunking and sentences of documents."""

from __future__ import annotations

import io
import itertools
from pathlib import Path

import pytest

from lorebase_scribe.extraction.parsing import (
    DOCX,
    MARKDOWN,
    PDF,
    ParseError,
    parse_document,
)
from lorebase_scribe.extraction.text import (
    chunk_text,
    heading_sections,
    is_heading,
    sentence_bounds,
)

DOCUMENTS = Path(__file__).resolve().parents[3] / "examples" / "supply" / "documents"


def sentences(text: str) -> list[str]:
    return [text[s:e] for s, e in sentence_bounds(text)]


def test_sentences_follow_markdown_structure() -> None:
    text = (
        "# Title C-1\n\nBetween Wexrix Filtration Sp. z o.o., Wroclaw\n(supplier S-1). "
        "It ends here.\n\n| A | B |\n| 1 | 2 |\n**Part:** P-1\n- item one\n- item two"
    )
    assert sentences(text) == [
        "# Title C-1",
        "Between Wexrix Filtration Sp. z o.o., Wroclaw\n(supplier S-1).",
        "It ends here.",
        "| A | B |",
        "| 1 | 2 |",
        "**Part:** P-1",
        "- item one",
        "- item two",
    ]
    assert is_heading(text, 3)
    assert not is_heading(text, 20)


def test_heading_sections_end_at_the_next_heading_of_the_same_level() -> None:
    text = "# A\nx\n## B\ny\n## C\nz\n# D\n"
    found = [(text[s.start : s.heading_end], text[s.start : s.end]) for s in heading_sections(text)]
    assert found == [
        ("# A", "# A\nx\n## B\ny\n## C\nz\n"),
        ("## B", "## B\ny\n"),
        ("## C", "## C\nz\n"),
        ("# D", "# D\n"),
    ]


def test_chunks_keep_offsets_size_and_overlap() -> None:
    text = (DOCUMENTS / "supplier-review-2025-q4.md").read_text()
    chunks = chunk_text(text, max_chars=400, overlap_chars=80)
    assert len(chunks) > 3
    for chunk in chunks:
        assert chunk.text == text[chunk.start : chunk.end]
        assert len(chunk.text) <= 400 + 80
        assert len(chunk.hash) == 32
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    for previous, current in itertools.pairwise(chunks):
        assert current.start <= previous.end  # overlapping or adjacent
        assert current.end > previous.end
    covered = {i for c in chunks for i in range(c.start, c.end)}
    assert all(i in covered for i, char in enumerate(text) if not char.isspace())


def test_small_documents_are_one_chunk_and_long_paragraphs_are_cut() -> None:
    text = (DOCUMENTS / "contract-c-3025-kavvik-filtration.md").read_text()
    assert len(chunk_text(text, max_chars=2000, overlap_chars=200)) == 1
    long = " ".join(["word"] * 300)
    pieces = chunk_text(long, max_chars=200, overlap_chars=0)
    assert all(len(p.text) <= 200 for p in pieces)
    assert " ".join(p.text for p in pieces) == long
    with pytest.raises(ValueError, match="overlap"):
        chunk_text(long, max_chars=200, overlap_chars=100)


def test_markdown_and_text_parse_natively() -> None:
    parsed = parse_document("﻿line one\r\nline two".encode(), MARKDOWN)
    assert parsed.text == "line one\nline two"
    with pytest.raises(ParseError, match="UTF-8"):
        parse_document(b"\xff\xfe\xfa", MARKDOWN)
    with pytest.raises(ParseError, match="unsupported"):
        parse_document(b"x", "image/png")


def test_pdf_text_layer_is_read() -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    parsed = parse_document(buffer.getvalue(), PDF)
    assert parsed.text == ""
    assert parsed.pages == (0,)
    assert "scanned" in parsed.warnings[0]
    with pytest.raises(ParseError, match="unreadable PDF"):
        parse_document(b"%PDF-1.4 broken", PDF)


def test_docx_paragraphs_headings_and_tables_are_read() -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("Agreement C-3025", level=1)
    document.add_paragraph("The agreement starts on 8 February 2024.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "S-1025"
    table.rows[0].cells[1].text = "74"
    buffer = io.BytesIO()
    document.save(buffer)
    parsed = parse_document(buffer.getvalue(), DOCX)
    assert parsed.text == (
        "# Agreement C-3025\n\nThe agreement starts on 8 February 2024.\n\n| S-1025 | 74 |"
    )
    with pytest.raises(ParseError, match="unreadable DOCX"):
        parse_document(b"PK\x03\x04 not a zip", DOCX)
