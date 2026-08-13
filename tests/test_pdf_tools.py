"""Tests para ``src.pdf_tools``.

Crea PDFs sintéticos en memoria con PyMuPDF y valida el resultado de
``parse_pdf`` y ``extract_title_heuristic``.
"""
from __future__ import annotations

import pymupdf

from src.pdf_tools import ParsedPdf, extract_title_heuristic, parse_pdf


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _make_pdf(pages: list[str]) -> bytes:
    """Genera un PDF con una página por cada string."""
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text)
    data = document.write()
    document.close()
    return data


# ---------------------------------------------------------------------------
# parse_pdf
# ---------------------------------------------------------------------------
def test_parse_pdf_extracts_text() -> None:
    pdf_bytes = _make_pdf(["Hola mundo"])
    parsed = parse_pdf(pdf_bytes, "demo.pdf")
    assert isinstance(parsed, ParsedPdf)
    assert parsed.has_text
    assert "Hola mundo" in parsed.text
    assert parsed.page_count == 1
    assert parsed.filename == "demo.pdf"


def test_parse_pdf_marks_empty_pdf_as_no_text() -> None:
    """Una página sin texto (solo imagen) no debe marcar ``has_text``."""
    document = pymupdf.open()
    document.new_page()  # página en blanco
    data = document.write()
    document.close()
    parsed = parse_pdf(data, "vacio.pdf")
    assert parsed.page_count == 1
    assert not parsed.has_text


def test_parse_pdf_includes_page_markers() -> None:
    pdf_bytes = _make_pdf(["Página uno", "Página dos"])
    parsed = parse_pdf(pdf_bytes, "pags.pdf")
    assert "[PAGE 1]" in parsed.text
    assert "[PAGE 2]" in parsed.text


def test_parse_pdf_extracts_doi() -> None:
    pdf_bytes = _make_pdf(["The DOI is 10.1234/abcd.5678 in this manuscript."])
    parsed = parse_pdf(pdf_bytes, "doi.pdf")
    assert parsed.doi == "10.1234/abcd.5678"


def test_parse_pdf_extracts_pmid() -> None:
    pdf_bytes = _make_pdf(["Registered as PMID: 12345678 in PubMed."])
    parsed = parse_pdf(pdf_bytes, "pmid.pdf")
    assert parsed.pmid == "12345678"


def test_fingerprint_is_stable_and_unique() -> None:
    pdf_a = _make_pdf(["A"])
    pdf_b = _make_pdf(["B"])
    parsed_a = parse_pdf(pdf_a, "a.pdf")
    parsed_a_again = parse_pdf(pdf_a, "a.pdf")
    parsed_b = parse_pdf(pdf_b, "b.pdf")

    assert parsed_a.fingerprint == parsed_a_again.fingerprint
    assert parsed_a.fingerprint != parsed_b.fingerprint
    assert len(parsed_a.fingerprint) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# extract_title_heuristic
# ---------------------------------------------------------------------------
def test_extract_title_returns_first_meaningful_line() -> None:
    text = "\n[PAGE 1]\nShort title that should be detected\nMore content"
    title = extract_title_heuristic(text)
    assert title is not None
    assert "Short title" in title


def test_extract_title_skips_short_lines() -> None:
    text = "Hi\n\nAnother line that is long enough to be considered"
    title = extract_title_heuristic(text)
    assert title is not None
    assert "Another line" in title


def test_extract_title_handles_empty_text() -> None:
    assert extract_title_heuristic("") is None
    assert extract_title_heuristic("\n\n") is None
