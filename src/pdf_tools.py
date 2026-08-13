"""Lectura local del PDF y extracción heurística de metadatos.

Usa PyMuPDF (``fitz``) para abrir el PDF, recorrer sus páginas y construir
un objeto :class:`ParsedPdf` con:

* ``text`` — concatenación del texto por página, con un separador
  ``\n[PAGE n]\n`` que luego aprovechará el extractor para referenciar
  páginas exactas en cada cita.
* ``doi`` / ``pmid`` — identificadores localizados en el texto (si están).
* ``fingerprint`` — SHA-256 de los bytes del PDF; permite reanalizar el
  mismo archivo sin duplicar expedientes.

Si el PDF es un escaneo sin capa de texto, ``text`` queda vacío y la
interfaz mostrará el aviso correspondiente; el módulo no intenta OCR.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable

import fitz  # PyMuPDF

from src.config import DOI_PATTERN, PMID_PATTERN

# Alias para mantener compatibilidad con `from src.pdf_tools import DOI_PATTERN`.
__all__ = ["DOI_PATTERN", "PMID_PATTERN", "ParsedPdf", "parse_pdf", "extract_title_heuristic"]


# ---------------------------------------------------------------------------
# Estructuras
# ---------------------------------------------------------------------------
@dataclass
class ParsedPdf:
    """Representa un PDF ya leído y normalizado para el resto del pipeline."""

    filename: str
    pages: list[dict[str, str]] = field(default_factory=list)
    text: str = ""
    doi: str | None = None
    pmid: str | None = None
    fingerprint: str = ""
    has_text: bool = False
    page_count: int = 0

    def snippets(self, max_chars: int = 220) -> Iterable[dict[str, str]]:
        """Fragmentos cortos por página (útil para mostrar extractos en la UI)."""
        for page in self.pages:
            if not page["text"]:
                continue
            text = page["text"].strip()
            if len(text) > max_chars:
                text = text[: max_chars - 1].rstrip() + "…"
            yield {"page": page["page"], "text": text}


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def parse_pdf(pdf_bytes: bytes, filename: str) -> ParsedPdf:
    """Abre un PDF en memoria y devuelve un :class:`ParsedPdf` normalizado.

    Lanza excepciones de PyMuPDF si el archivo no es un PDF válido o está
    protegido. La capa superior captura los errores genéricos para no
    exponer detalles internos al usuario final.
    """
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: list[dict[str, str]] = []
    text_chunks: list[str] = []

    try:
        for index, page in enumerate(document, start=1):
            page_text = page.get_text("text") or ""
            page_text = page_text.strip()
            pages.append({"page": str(index), "text": page_text})
            if page_text:
                text_chunks.append(f"\n[PAGE {index}]\n{page_text}")
    finally:
        document.close()

    full_text = "".join(text_chunks)
    doi = _first_doi(full_text)
    pmid = _first_pmid(full_text)

    return ParsedPdf(
        filename=filename,
        pages=pages,
        text=full_text,
        doi=doi,
        pmid=pmid,
        fingerprint=hashlib.sha256(pdf_bytes).hexdigest(),
        has_text=bool(full_text.strip()),
        page_count=len(pages),
    )


def extract_title_heuristic(text: str) -> str | None:
    """Heurística simple: la primera línea no vacía con suficiente longitud."""
    if not text:
        return None
    # Ignoramos el encabezado de página que añade parse_pdf.
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("[PAGE"):
            continue
        if len(line) < 10:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        return line[:240]
    return None


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------
def _first_doi(text: str) -> str | None:
    """Devuelve el primer DOI encontrado o ``None``."""
    if not text:
        return None
    match = DOI_PATTERN.search(text)
    if not match:
        return None
    # Limpia puntuación final común (puntos, comas, paréntesis).
    return match.group(1).rstrip(".,);")


def _first_pmid(text: str) -> str | None:
    if not text:
        return None
    match = PMID_PATTERN.search(text)
    return match.group(1) if match else None
