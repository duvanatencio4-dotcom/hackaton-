"""Tests para ``src.config``.

Validan constantes, patrones de identificadores y carga segura del
entorno. No hacen red ni escriben en la base de datos.
"""
from __future__ import annotations

import os

import pytest

from src import config
from src.config import DOI_PATTERN, PMID_PATTERN
from src.metadata_services import canonical_doi, canonical_pmid


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
def test_app_name_is_set() -> None:
    assert config.APP_NAME
    assert isinstance(config.APP_NAME, str)


def test_topic_presets_contains_common_categories() -> None:
    assert "Sueño" in config.TOPIC_PRESETS
    assert "Ansiedad" in config.TOPIC_PRESETS


def test_human_decision_values_are_complete() -> None:
    for value in ("Pendiente", "Aprobar", "Rechazar"):
        assert value in config.HUMAN_DECISION_VALUES


def test_human_review_notice_mentions_human_role() -> None:
    assert "revisora" in config.HUMAN_REVIEW_NOTICE.lower() or "revisor" in config.HUMAN_REVIEW_NOTICE.lower()


def test_request_timeout_is_positive() -> None:
    assert config.REQUEST_TIMEOUT > 0


def test_max_upload_mb_is_positive() -> None:
    assert config.MAX_UPLOAD_MB > 0


# ---------------------------------------------------------------------------
# Patrones
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("10.1234/abcd.5678", "10.1234/abcd.5678"),
        ("doi:10.1234/abcd.5678", "10.1234/abcd.5678"),
        ("https://doi.org/10.1234/abcd.5678", "10.1234/abcd.5678"),
        ("See 10.1234/abcd.5678 in the references.", "10.1234/abcd.5678"),
        ("Plain text without identifiers", None),
        ("", None),
    ],
)
def test_canonical_doi(text: str, expected: str | None) -> None:
    assert canonical_doi(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("PMID: 12345678", "12345678"),
        ("https://pubmed.ncbi.nlm.nih.gov/99999999/", "99999999"),
        ("Sin identificador", None),
    ],
)
def test_canonical_pmid(text: str, expected: str | None) -> None:
    assert canonical_pmid(text) == expected


def test_doi_pattern_matches_minimum_digits() -> None:
    """La spec exige al menos 4 dígitos tras el punto."""
    # 4 dígitos es válido
    assert DOI_PATTERN.search("10.1234/abc") is not None
    # 3 dígitos no
    assert DOI_PATTERN.search("10.123/abc") is None


def test_pmid_pattern_matches_range() -> None:
    assert PMID_PATTERN.search("PMID: 12345") is not None
    assert PMID_PATTERN.search("PMID: 1234") is None  # necesita 5-9 dígitos


# ---------------------------------------------------------------------------
# Carga de entorno
# ---------------------------------------------------------------------------
def test_llm_disabled_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import importlib
    reloaded = importlib.reload(config)
    assert reloaded.LLM_ENABLED is False
    assert reloaded.GEMINI_ENABLED is False
    assert reloaded.LLM_API_KEY == ""


def test_llm_enabled_when_api_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    import importlib
    reloaded = importlib.reload(config)
    try:
        assert reloaded.LLM_ENABLED is True
        assert reloaded.LLM_API_KEY == "test-key-not-real"
    finally:
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        importlib.reload(config)


def test_gemini_enabled_when_gemini_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    import importlib
    reloaded = importlib.reload(config)
    try:
        assert reloaded.GEMINI_ENABLED is True
        assert reloaded.GEMINI_API_KEY == "test-gemini-key"
    finally:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        importlib.reload(config)
