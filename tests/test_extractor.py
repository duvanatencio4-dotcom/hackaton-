"""Tests para ``src.extractor``.

Se valida:

* El modo local produce hallazgos con citas literales existentes en el PDF.
* El modo con API (stub) solo acepta hallazgos cuyas citas aparezcan
  realmente en el PDF (descarta invenciones).
* El extractor devuelve ``None`` cuando no hay texto extraíble.
"""
from __future__ import annotations

from typing import Any

import pymupdf
import pytest

from src import extractor
from src.extractor import extract_reviewable_findings
from src.pdf_tools import parse_pdf


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _make_pdf(pages: list[str]) -> bytes:
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text)
    data = document.write()
    document.close()
    return data


def _sample_pdf() -> bytes:
    return _make_pdf([
        "Effect of sleep on anxiety: a randomized study.\n"
        "Methods: 120 participants were randomized to intervention or control.\n"
        "Results: anxiety scores decreased by 18% in the intervention group versus 4% in control.\n"
        "The study had a small sample and short follow-up. Self-report measures were used."
    ])


# ---------------------------------------------------------------------------
# Modo local
# ---------------------------------------------------------------------------
def test_returns_none_for_empty_pdf() -> None:
    document = pymupdf.open()
    document.new_page()
    data = document.write()
    document.close()
    parsed = parse_pdf(data, "vacio.pdf")
    assert extract_reviewable_findings(parsed, "X") is None


def test_returns_none_when_no_parsed() -> None:
    assert extract_reviewable_findings(None, "X") is None


def test_local_mode_generates_findings_with_literal_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extractor, "LLM_ENABLED", False)
    parsed = parse_pdf(_sample_pdf(), "demo.pdf")
    result = extract_reviewable_findings(parsed, "Sleep & anxiety study")

    assert result is not None
    assert result["llm_used"] is False
    assert result["findings"], "Debe generar al menos un hallazgo"
    assert "sugerencia" in result["guardrail"].lower() or "literal" in result["guardrail"].lower()

    # Cada cita debe aparecer literalmente en el texto del PDF
    for finding in result["findings"]:
        assert finding["status"] == "Pendiente de revisión"
        assert finding["evidence"], "Cada hallazgo debe tener al menos una evidencia"
        for evidence in finding["evidence"]:
            assert "page" in evidence
            assert evidence["quote"] in parsed.text, (
                f"La cita {evidence['quote']!r} no aparece en el PDF"
            )


def test_local_mode_infers_study_design(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "LLM_ENABLED", False)
    parsed = parse_pdf(_sample_pdf(), "demo.pdf")
    result = extract_reviewable_findings(parsed, "X")
    assert result is not None
    assert "ensayo" in result["study_design_suggestion"].lower() or "rct" in result["study_design_suggestion"].lower()


def test_local_mode_detects_limitations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "LLM_ENABLED", False)
    parsed = parse_pdf(_sample_pdf(), "demo.pdf")
    result = extract_reviewable_findings(parsed, "X")
    assert result is not None
    notes = result["limitations_note"].lower()
    assert "muestra" in notes or "seguimiento" in notes or "autoreporte" in notes


# ---------------------------------------------------------------------------
# Modo con API (stub)
# ---------------------------------------------------------------------------
class _FakeLLMResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("error")

    def json(self) -> dict[str, Any]:
        return self._payload


def test_llm_mode_accepts_valid_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "LLM_ENABLED", True)
    monkeypatch.setattr(extractor, "LLM_API_KEY", "fake-key")
    monkeypatch.setattr(extractor, "LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(extractor, "LLM_MODEL", "test-model")

    parsed = parse_pdf(_sample_pdf(), "demo.pdf")
    sentences = extractor._sentence_page_map(parsed)
    # El LLM cita los índices 1 y 2 (oraciones existentes)
    payload = {
        "choices": [{
            "message": {
                "content": '{"findings": [{"claim": "El sueño reduce la ansiedad.", '
                           '"evidence_indices": [1, 2]}]}'
            }
        }]
    }
    monkeypatch.setattr(
        extractor.requests, "post",
        lambda *a, **kw: _FakeLLMResponse(payload),
    )

    result = extract_reviewable_findings(parsed, "X")
    assert result is not None
    assert result["llm_used"] is True
    assert len(result["findings"]) == 1
    assert "ansiedad" in result["findings"][0]["claim"].lower()


def test_llm_mode_rejects_invented_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si el modelo cita texto inexistente, se cae al modo local."""
    monkeypatch.setattr(extractor, "LLM_ENABLED", True)
    monkeypatch.setattr(extractor, "LLM_API_KEY", "fake-key")
    monkeypatch.setattr(extractor, "LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(extractor, "LLM_MODEL", "test-model")

    parsed = parse_pdf(_sample_pdf(), "demo.pdf")
    payload = {
        "choices": [{
            "message": {
                "content": '{"findings": [{"claim": "Algo inventado.", '
                           '"evidence_indices": [999]}]}'
            }
        }]
    }
    monkeypatch.setattr(
        extractor.requests, "post",
        lambda *a, **kw: _FakeLLMResponse(payload),
    )

    result = extract_reviewable_findings(parsed, "X")
    # Sin citas válidas, debe caer al modo local
    assert result is not None
    assert result["llm_used"] is False


def test_llm_mode_falls_back_on_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "LLM_ENABLED", True)
    monkeypatch.setattr(extractor, "LLM_API_KEY", "fake-key")
    monkeypatch.setattr(extractor, "LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(extractor, "LLM_MODEL", "test-model")

    def _raise(*a: Any, **kw: Any) -> None:
        raise extractor.requests.RequestException("offline")

    monkeypatch.setattr(extractor.requests, "post", _raise)

    parsed = parse_pdf(_sample_pdf(), "demo.pdf")
    result = extract_reviewable_findings(parsed, "X")
    assert result is not None
    assert result["llm_used"] is False
    assert result["findings"]  # Modo local igualmente produce hallazgos
