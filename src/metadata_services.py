"""Servicios de metadatos públicos.

Cada fuente es consultada de forma independiente y tolerante a fallos:

* **Crossref** — utiliza el endpoint REST ``/works/{{doi}}`` y revisa el
  campo ``update-to`` descrito por Crossref para distribuir datos de
  Retraction Watch.
* **PubMed (NCBI E-utilities)** — primero ``esummary`` y, si hay
  abstract, ``efetch`` en formato ``abstract``. Se apoya en la
  documentación oficial de las E-utilities.
* **OpenAlex** — contraste adicional con el campo ``is_retracted``.

Los resultados se agregan en un único diccionario. Una alerta **nunca**
es un veredicto: solo señala qué comprobar en la fuente primaria.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree

import requests

from src.config import (
    CROSSREF_API,
    DOI_PATTERN,
    OPENALEX_API,
    PMID_PATTERN,
    PUBMED_EFETCH,
    PUBMED_ESUMMARY,
    REQUEST_TIMEOUT,
    USER_AGENT,
)


# ---------------------------------------------------------------------------
# Normalización de identificadores
# ---------------------------------------------------------------------------
def canonical_doi(value: str | None) -> str | None:
    """Extrae un DOI canónico de una cadena arbitraria (URL, ``doi:``, texto)."""
    if not value:
        return None
    match = DOI_PATTERN.search(value)
    if not match:
        return None
    return match.group(1).rstrip(".,);")


def canonical_pmid(value: str | None) -> str | None:
    if not value:
        return None
    value_clean = value.strip()
    if re.fullmatch(r"\d{5,9}", value_clean):
        return value_clean
    match = PMID_PATTERN.search(value_clean)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Construcción de alertas
# ---------------------------------------------------------------------------
def _alert(level: str, label: str, detail: str, source: str) -> dict[str, str]:
    return {"level": level, "label": label, "detail": detail, "source": source}


# ---------------------------------------------------------------------------
# Funciones de apoyo para peticiones HTTP
# ---------------------------------------------------------------------------
def _http_get(url: str, *, params: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None) -> requests.Response | None:
    """Realiza una petición GET tolerante a fallos de red. Devuelve ``None`` si falla."""
    merged_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        merged_headers.update(headers)
    try:
        response = requests.get(
            url,
            params=params,
            headers=merged_headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response
    except requests.RequestException:
        return None


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------
def _query_crossref(doi: str | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Devuelve metadatos y alertas generados a partir de Crossref."""
    empty: dict[str, Any] = {}
    alerts: list[dict[str, str]] = []
    if not doi:
        return empty, alerts

    response = _http_get(CROSSREF_API.format(doi=doi))
    if response is None:
        alerts.append(_alert(
            "info",
            "Crossref no respondió",
            "No se pudo consultar Crossref para este DOI. El análisis local continúa.",
            "Crossref REST API",
        ))
        return empty, alerts

    payload = response.json().get("message") or {}
    title_list = payload.get("title") or []
    container_list = payload.get("container-title") or []
    issued = payload.get("issued", {}).get("date-parts", [[None]])[0]
    published = "-".join(str(part) for part in issued if part) if issued else None

    record = {
        "doi": payload.get("DOI") or doi,
        "title": title_list[0] if title_list else None,
        "journal": container_list[0] if container_list else None,
        "published": published,
        "url": payload.get("URL"),
        "publisher": payload.get("publisher"),
        "type": payload.get("type"),
    }

    # Revisa relaciones de actualización. Crossref documenta que las
    # retractaciones aparecen en ``update-to``.
    for update in payload.get("update-to", []) or []:
        update_type = (update.get("type") or "").lower()
        if "retract" in update_type:
            alerts.append(_alert(
                "alta",
                "Posible retractación registrada por Crossref",
                f"Tipo declarado: {update.get('type')}. "
                f"DOI relacionado: {update.get('DOI') or 'no especificado'}.",
                "Crossref / Retraction Watch",
            ))
        elif "correction" in update_type:
            alerts.append(_alert(
                "media",
                "Corrección editorial publicada",
                f"Tipo declarado: {update.get('type')}. "
                f"DOI relacionado: {update.get('DOI') or 'no especificado'}.",
                "Crossref",
            ))
        elif "expression" in update_type or "withdrawal" in update_type:
            alerts.append(_alert(
                "media",
                "Cambio editorial reportado",
                f"Tipo declarado: {update.get('type')}. Verifique la nota editorial original.",
                "Crossref",
            ))

    # Estado del propio recurso.
    if (payload.get("is-update") or payload.get("updated-by")) and not alerts:
        alerts.append(_alert(
            "info",
            "Actualizaciones registradas",
            "Crossref reporta otras versiones o actualizaciones. Revise la nota editorial.",
            "Crossref",
        ))

    return record, alerts


# ---------------------------------------------------------------------------
# PubMed (E-utilities)
# ---------------------------------------------------------------------------
def _query_pubmed(pmid: str | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    empty: dict[str, Any] = {}
    alerts: list[dict[str, str]] = []
    if not pmid:
        return empty, alerts

    summary = _http_get(
        PUBMED_ESUMMARY,
        params={"db": "pubmed", "id": pmid, "retmode": "json"},
    )
    if summary is None:
        alerts.append(_alert(
            "info",
            "PubMed no respondió",
            "No se pudo consultar el registro PubMed. Verifique manualmente si lo necesita.",
            "NCBI E-utilities",
        ))
        return empty, alerts

    result = (summary.json().get("result") or {}).get(pmid) or {}
    if not result:
        alerts.append(_alert(
            "info",
            "PMID no localizado en PubMed",
            "Verifique que el identificador sea correcto.",
            "NCBI E-utilities",
        ))
        return empty, alerts

    record = {
        "pmid": pmid,
        "title": result.get("title"),
        "journal": result.get("source") or result.get("fulljournalname"),
        "published": result.get("pubdate"),
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "publication_types": list(result.get("pubtype") or []),
    }

    # Abstract opcional: efetch en formato abstract.
    abstract = _fetch_pubmed_abstract(pmid)
    if abstract:
        record["abstract"] = abstract
    else:
        record["abstract"] = None

    pub_types_lower = {t.lower() for t in record["publication_types"]}
    if any("retract" in t for t in pub_types_lower):
        alerts.append(_alert(
            "alta",
            "Tipo de publicación: retraction",
            "PubMed clasifica el registro como retractación. Verifique el aviso editorial.",
            "PubMed",
        ))
    if any("correction" in t or "erratum" in t for t in pub_types_lower):
        alerts.append(_alert(
            "media",
            "Corrección o erratum detectado",
            "PubMed indica que existe una corrección o erratum vinculado.",
            "PubMed",
        ))
    if any("withdrawn" in t for t in pub_types_lower):
        alerts.append(_alert(
            "alta",
            "Artículo marcado como retirado",
            "PubMed registra el artículo como retirado. Revise la nota editorial.",
            "PubMed",
        ))

    return record, alerts


def _fetch_pubmed_abstract(pmid: str) -> str | None:
    response = _http_get(
        PUBMED_EFETCH,
        params={"db": "pubmed", "id": pmid, "retmode": "xml", "rettype": "abstract"},
        headers={"Accept": "application/xml"},
    )
    if response is None:
        return None
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        return None

    parts: list[str] = []
    for abstract in root.findall(".//Abstract/AbstractText"):
        label = abstract.attrib.get("Label")
        text = "".join(abstract.itertext()).strip()
        if not text:
            continue
        parts.append(f"{label}: {text}" if label else text)
    return "\n\n".join(parts) if parts else None


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------
def _query_openalex(doi: str | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    empty: dict[str, Any] = {}
    alerts: list[dict[str, str]] = []
    if not doi:
        return empty, alerts

    response = _http_get(OPENALEX_API.format(doi=doi))
    if response is None:
        alerts.append(_alert(
            "info",
            "OpenAlex no respondió",
            "No se pudo obtener el registro de OpenAlex. Esto no invalida el resto del análisis.",
            "OpenAlex",
        ))
        return empty, alerts

    payload = response.json() or {}
    is_retracted = bool(payload.get("is_retracted"))
    record = {
        "is_retracted": is_retracted,
        "open_access": payload.get("open_access"),
        "publication_year": payload.get("publication_year"),
        "cited_by_count": payload.get("cited_by_count"),
        "landing_page_url": payload.get("primary_location", {}).get("landing_page_url")
        or payload.get("doi_url"),
    }

    if is_retracted:
        alerts.append(_alert(
            "alta",
            "OpenAlex marca el trabajo como retractado",
            "El campo is_retracted es verdadero. Confirme con el aviso editorial del editor.",
            "OpenAlex",
        ))

    return record, alerts


# ---------------------------------------------------------------------------
# Inspección agregada
# ---------------------------------------------------------------------------
def inspect_study(
    doi: str | None,
    pmid: str | None,
    local_title: str | None,
    pdf_text_status: bool | None,
) -> dict[str, Any]:
    """Coordina las tres fuentes y devuelve un único diccionario para la UI."""
    canonical = canonical_doi(doi) if doi else None
    pmid_value = canonical_pmid(pmid) if pmid else None

    crossref, crossref_alerts = _query_crossref(canonical)
    pubmed, pubmed_alerts = _query_pubmed(pmid_value)
    openalex, openalex_alerts = _query_openalex(canonical)

    alerts: list[dict[str, str]] = []
    alerts.extend(crossref_alerts)
    alerts.extend(pubmed_alerts)
    alerts.extend(openalex_alerts)

    # Señales derivadas del propio PDF / identificadores.
    if not canonical and not pmid_value:
        alerts.append(_alert(
            "media",
            "Sin DOI ni PMID confirmables",
            "No fue posible identificar el documento en las bases públicas. "
            "Confirme manualmente que el PDF corresponde al estudio que se pretende evaluar.",
            "Análisis local",
        ))

    if pdf_text_status is False:
        alerts.append(_alert(
            "media",
            "PDF sin texto extraíble",
            "El archivo parece un escaneo sin capa de texto. "
            "Aplique OCR antes de usar el extractor de hallazgos.",
            "Análisis local",
        ))

    if local_title and crossref.get("title"):
        # Heurística: si los primeros 40 caracteres no coinciden, avisar.
        a = re.sub(r"\W+", "", local_title.lower())[:40]
        b = re.sub(r"\W+", "", (crossref["title"] or "").lower())[:40]
        if a and b and a != b:
            alerts.append(_alert(
                "info",
                "El título local no coincide con el de Crossref",
                "Verifique que el PDF subido corresponda al DOI declarado.",
                "Análisis local",
            ))

    return {
        "doi": canonical,
        "pmid": pmid_value,
        "local_title": local_title,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "crossref": crossref,
        "pubmed": pubmed,
        "openalex": openalex,
        "alerts": alerts,
    }
