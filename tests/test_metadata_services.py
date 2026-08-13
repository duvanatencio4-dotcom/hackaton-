"""Tests para ``src.metadata_services``.

Los tests no hacen red: interceptan ``requests.get`` con
``monkeypatch`` y devuelven respuestas simuladas. Esto permite validar
cómo ``inspect_study`` agrega información de Crossref, PubMed y OpenAlex
ante distintos escenarios (éxito, fallo de red, retractación, etc.).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src import metadata_services
from src.metadata_services import canonical_doi, canonical_pmid, inspect_study


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, *, json_data: dict[str, Any] | None = None,
                 content: bytes = b"", status_code: int = 200) -> None:
        self._json = json_data
        self.content = content
        self.status_code = status_code
        self.url = ""

    def json(self) -> dict[str, Any]:
        if self._json is None:
            raise ValueError("Sin cuerpo JSON")
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise metadata_services.requests.RequestException("error simulado")


def _patch_requests(monkeypatch: pytest.MonkeyPatch, responder) -> None:
    """Reemplaza ``metadata_services.requests.get`` por el callable ``responder``."""
    monkeypatch.setattr(metadata_services.requests, "get", responder)


# ---------------------------------------------------------------------------
# canonical_doi / canonical_pmid
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("doi:10.1234/foo", "10.1234/foo"),
        ("https://doi.org/10.1234/foo.", "10.1234/foo"),
        ("10.9999/x-y_z", "10.9999/x-y_z"),
        ("", None),
        ("no doi here", None),
    ],
)
def test_canonical_doi_variants(text: str, expected: str | None) -> None:
    assert canonical_doi(text) == expected


def test_canonical_pmid_extracts_digits() -> None:
    assert canonical_pmid("PMID: 12345678") == "12345678"
    assert canonical_pmid("https://pubmed.ncbi.nlm.nih.gov/99999999/") == "99999999"
    assert canonical_pmid("") is None


# ---------------------------------------------------------------------------
# inspect_study - escenarios de éxito
# ---------------------------------------------------------------------------
def _crossref_ok(doi: str) -> _FakeResponse:
    return _FakeResponse(json_data={
        "message": {
            "DOI": doi,
            "title": ["A study of sleep"],
            "container-title": ["Journal of Sleep Research"],
            "issued": {"date-parts": [[2024, 1, 15]]},
            "URL": f"https://doi.org/{doi}",
            "publisher": "Doma Press",
        }
    })


def _crossref_retracted(doi: str) -> _FakeResponse:
    return _FakeResponse(json_data={
        "message": {
            "DOI": doi,
            "title": ["A study of sleep"],
            "container-title": ["Journal of Sleep Research"],
            "issued": {"date-parts": [[2024]]},
            "URL": f"https://doi.org/{doi}",
            "update-to": [{
                "type": "retraction",
                "DOI": "10.1234/retraction-notice",
            }],
        }
    })


def _pubmed_ok(pmid: str) -> _FakeResponse:
    return _FakeResponse(json_data={
        "result": {
            pmid: {
                "title": "A study of sleep",
                "source": "J Sleep Res",
                "pubdate": "2024 Jan 15",
                "pubtype": ["Journal Article", "Randomized Controlled Trial"],
            }
        }
    })


def _openalex_ok(doi: str, retracted: bool = False) -> _FakeResponse:
    return _FakeResponse(json_data={
        "doi": doi,
        "is_retracted": retracted,
        "publication_year": 2024,
        "cited_by_count": 12,
        "primary_location": {"landing_page_url": f"https://example.org/{doi}"},
    })


def _make_router(monkeypatch: pytest.MonkeyPatch, table: dict[str, _FakeResponse]) -> None:
    """Devuelve una respuesta distinta según la URL solicitada."""
    def _route(url: str, *args: Any, **kwargs: Any) -> _FakeResponse:
        for key, response in table.items():
            if key in url:
                return response
        return _FakeResponse(status_code=404)
    _patch_requests(monkeypatch, _route)


def test_inspect_study_aggregates_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    doi = "10.1234/ok.1234"
    pmid = "12345678"
    table = {
        "api.crossref.org": _crossref_ok(doi),
        "esummary.fcgi": _pubmed_ok(pmid),
        "efetch.fcgi": _FakeResponse(content=b"<PubmedArticleSet></PubmedArticleSet>"),
        "api.openalex.org": _openalex_ok(doi, retracted=False),
    }
    _make_router(monkeypatch, table)

    result = inspect_study(doi, pmid, "Local Title", True)
    assert result["doi"] == doi
    assert result["pmid"] == pmid
    assert result["crossref"]["title"] == "A study of sleep"
    assert result["crossref"]["journal"] == "Journal of Sleep Research"
    assert result["pubmed"]["title"] == "A study of sleep"
    assert result["openalex"]["is_retracted"] is False
    assert result["checked_at"]


def test_inspect_study_detects_retraction(monkeypatch: pytest.MonkeyPatch) -> None:
    doi = "10.1234/retr.1234"
    pmid = "22222222"
    table = {
        "api.crossref.org": _crossref_retracted(doi),
        "esummary.fcgi": _pubmed_ok(pmid),
        "efetch.fcgi": _FakeResponse(content=b"<PubmedArticleSet></PubmedArticleSet>"),
        "api.openalex.org": _openalex_ok(doi, retracted=True),
    }
    _make_router(monkeypatch, table)

    result = inspect_study(doi, pmid, "Local", True)
    levels = {alert["level"] for alert in result["alerts"]}
    labels = {alert["label"] for alert in result["alerts"]}
    assert "alta" in levels
    assert any("retract" in label.lower() for label in labels)


def test_inspect_study_handles_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si todas las fuentes fallan, no se rompe y devuelve alertas informativas."""
    def _fail(*args: Any, **kwargs: Any) -> None:
        raise metadata_services.requests.RequestException("offline")
    _patch_requests(monkeypatch, _fail)

    result = inspect_study("10.1234/off.1234", None, None, None)
    # Debe seguir devolviendo estructura
    assert result["doi"] == "10.1234/off.1234"
    assert result["alerts"]  # al menos una alerta de "sin DOI/PMID" o "no respondió"
    assert result["crossref"] == {}
    assert result["pubmed"] == {}
    assert result["openalex"] == {}


def test_inspect_study_warns_when_text_not_extractable(monkeypatch: pytest.MonkeyPatch) -> None:
    _make_router(monkeypatch, {
        "api.crossref.org": _crossref_ok("10.1234/scan.1234"),
        "esummary.fcgi": _FakeResponse(json_data={"result": {}}),
        "api.openalex.org": _openalex_ok("10.1234/scan.1234"),
    })

    result = inspect_study("10.1234/scan.1234", None, None, pdf_text_status=False)
    labels = {alert["label"] for alert in result["alerts"]}
    assert any("escaneo" in label.lower() or "extra" in label.lower() for label in labels)


def test_inspect_study_flags_missing_doi_and_pmid(monkeypatch: pytest.MonkeyPatch) -> None:
    _make_router(monkeypatch, {})
    result = inspect_study(None, None, "Local", True)
    labels = {alert["label"] for alert in result["alerts"]}
    assert any("doi" in label.lower() or "pmid" in label.lower() for label in labels)
