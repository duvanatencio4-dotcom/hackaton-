"""Tests para ``src.verifier`` (Verificación de afirmaciones con Gemini).

Valida el formateo de prompts, extracción de JSON, manejo de respuestas
mockeadas de Google Search Grounding y degradación limpia ante errores.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from src import verifier
from src.verifier import _extract_json_from_response, verify_claims_with_gemini


def test_extract_json_direct() -> None:
    data = {"overview": "test", "verifications": [{"claim": "c1", "verdict": "RESPALDADO"}]}
    raw = json.dumps(data)
    result = _extract_json_from_response(raw)
    assert result == data


def test_extract_json_from_markdown_block() -> None:
    raw = 'Aquí está el resultado:\n```json\n{"overview": "resumen", "verifications": []}\n```'
    result = _extract_json_from_response(raw)
    assert result is not None
    assert result["overview"] == "resumen"


def test_extract_json_empty_or_invalid() -> None:
    assert _extract_json_from_response("") is None
    assert _extract_json_from_response("no json here at all") is None


def test_verify_claims_disabled_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verifier, "GEMINI_API_KEY", "")
    result = verify_claims_with_gemini(["El té verde reduce el estrés"], title="Test", api_key="")
    assert result["success"] is False
    assert "GEMINI_API_KEY" in result["error"]


def test_verify_claims_empty_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verifier, "GEMINI_API_KEY", "dummy-key")
    result = verify_claims_with_gemini([], title="Test")
    assert result["success"] is False
    assert "No se proporcionaron" in result["error"]


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


def test_verify_claims_successful_grounding(monkeypatch: pytest.MonkeyPatch) -> None:
    gemini_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps({
                                "overview": "La evidencia respalda el efecto del magnesio.",
                                "verifications": [
                                    {
                                        "claim": "El magnesio mejora el sueño.",
                                        "verdict": "RESPALDADO",
                                        "summary": "Múltiples ensayos clínicos lo respaldan.",
                                        "supporting_points": "Estudios en PubMed 2021-2023.",
                                        "caveats_or_conflict": "Dosis altas causan malestar.",
                                    }
                                ],
                            })
                        }
                    ]
                },
                "groundingMetadata": {
                    "webSearchQueries": ["magnesio sueño ensayo clinico"],
                    "groundingChunks": [
                        {
                            "web": {
                                "uri": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                                "title": "Magnesium for sleep quality",
                            }
                        }
                    ],
                },
            }
        ]
    }

    monkeypatch.setattr(
        verifier.requests,
        "post",
        lambda *a, **kw: _FakeResponse(gemini_payload),
    )

    result = verify_claims_with_gemini(
        ["El magnesio mejora el sueño."],
        title="Estudio Magnesio",
        api_key="test-key",
    )

    assert result["success"] is True
    assert result["overview"] == "La evidencia respalda el efecto del magnesio."
    assert len(result["verifications"]) == 1
    assert result["verifications"][0]["verdict"] == "RESPALDADO"
    assert len(result["sources"]) == 1
    assert result["sources"][0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert "magnesio sueño ensayo clinico" in result["search_queries"]


def test_verify_claims_handles_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_error(*a, **kw):
        import requests
        raise requests.ConnectionError("Fallo de red")

    monkeypatch.setattr(verifier.requests, "post", _raise_error)

    result = verify_claims_with_gemini(
        ["Afirmación"],
        title="Test",
        api_key="test-key",
    )

    assert result["success"] is False
    assert "Error al conectar" in result["error"]
