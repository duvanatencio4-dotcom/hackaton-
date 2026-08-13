"""Extractor de hallazgos con citas literales validadas.

Este módulo es la única parte del MVP que podría invocar una API
externa; aun así, **nunca** inventa texto: cada evidencia debe
aparecer literalmente en el PDF analizado.

Funcionamiento:

1. Divide el texto del PDF en oraciones y descarta las que no aportan
   información (encabezados muy cortos, números de página sueltos, etc.).
2. Puntúa cada oración según su longitud y densidad léxica para
   priorizar hallazgos plausibles.
3. Construye ``findings`` agrupando oraciones por afinidad de palabras
   clave. Cada hallazgo lleva **al menos** una evidencia literal con su
   número de página.
4. Si hay API configurada (``src.config.LLM_ENABLED``), le pide al
   modelo que organice las oraciones en afirmaciones más legibles. Si
   el modelo responde con citas que no aparecen en el PDF, se descartan
   y se devuelve la versión local.
5. Si no hay API, opera completamente en local.

El resultado siempre expone un ``guardrail`` que se muestra al usuario
para recordar que las afirmaciones son **sugerencias**, no veredictos.
"""
from __future__ import annotations

import json
import re
import textwrap
from collections import Counter
from typing import Any

import requests

from src.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_ENABLED,
    LLM_MODEL,
    LLM_TEMPERATURE,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from src.pdf_tools import ParsedPdf


# ---------------------------------------------------------------------------
# Guardrail visible para la UI
# ---------------------------------------------------------------------------
GUARDRAIL = (
    "Las afirmaciones siguientes son sugerencias derivadas del PDF. "
    "Cada cita muestra el texto literal y la página; revise la fuente "
    "primaria antes de usar la información. Ningún hallazgo sustituye "
    "una lectura crítica."
)


# ---------------------------------------------------------------------------
# Extracción de oraciones
# ---------------------------------------------------------------------------
_SENTENCE_SPLIT = re.compile(r"(?<=[\.\!\?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡])")


def _split_sentences(text: str) -> list[str]:
    """Divide un texto en oraciones y descarta fragmentos sin contenido."""
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return []
    raw = _SENTENCE_SPLIT.split(cleaned)
    return [s.strip() for s in raw if len(s.strip()) >= 30]


def _sentence_page_map(parsed: ParsedPdf) -> list[tuple[str, str]]:
    """Devuelve tuplas ``(oración, página)`` preservando el orden de aparición."""
    result: list[tuple[str, str]] = []
    for page in parsed.pages:
        for sentence in _split_sentences(page["text"]):
            result.append((sentence, page["page"]))
    return result


# ---------------------------------------------------------------------------
# Heurísticas de selección
# ---------------------------------------------------------------------------
_FILLER_TOKENS = {
    "the", "and", "for", "with", "that", "from", "this", "these", "those",
    "los", "las", "del", "que", "con", "para", "por", "una", "uno", "como",
    "sobre", "entre", "their", "have", "has", "were", "been", "study",
    "estudio", "results", "resultados", "methods", "method", "methodology",
}


def _keywords(text: str) -> set[str]:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{4,}", text.lower())
    return {t for t in tokens if t not in _FILLER_TOKENS}


def _score_sentence(sentence: str) -> float:
    """Puntúa según longitud útil y presencia de cifras o cifras clave."""
    words = sentence.split()
    if len(words) < 8 or len(words) > 70:
        return 0.0
    has_number = bool(re.search(r"\d", sentence))
    unique_ratio = len({w.lower() for w in words}) / max(len(words), 1)
    base = min(len(words) / 30.0, 1.0) * 0.6 + unique_ratio * 0.4
    return base + 0.2 if has_number else base


# ---------------------------------------------------------------------------
# Construcción de hallazgos en modo local
# ---------------------------------------------------------------------------
def _local_findings(parsed: ParsedPdf, title: str) -> dict[str, Any]:
    """Selecciona oraciones representativas y las agrupa en hallazgos."""
    sentences = _sentence_page_map(parsed)
    if not sentences:
        return {
            "llm_used": False,
            "guardrail": GUARDRAIL,
            "findings": [],
            "study_design_suggestion": _design_suggestion(parsed, []),
            "limitations_note": _limitations_note(parsed, []),
        }

    ranked = sorted(
        sentences,
        key=lambda item: _score_sentence(item[0]),
        reverse=True,
    )

    # Agrupamos por palabras clave; cada hallazgo reune hasta 2 oraciones
    # que comparten vocabulario significativo.
    groups: list[list[tuple[str, str]]] = []
    used: set[int] = set()
    for idx, (sentence, page) in enumerate(ranked):
        if idx in used:
            continue
        cluster = [(sentence, page)]
        used.add(idx)
        own_keys = _keywords(sentence)
        if not own_keys:
            continue
        for jdx in range(idx + 1, len(ranked)):
            if jdx in used:
                continue
            other_sentence, other_page = ranked[jdx]
            shared = own_keys & _keywords(other_sentence)
            if len(shared) >= 2:
                cluster.append((other_sentence, other_page))
                used.add(jdx)
                if len(cluster) >= 2:
                    break
        groups.append(cluster)

    findings: list[dict[str, Any]] = []
    for cluster in groups[:6]:
        claim = _summarise_cluster(cluster, title)
        evidence = [
            {"quote": _truncate(sentence, 320), "page": page}
            for sentence, page in cluster
        ]
        findings.append({
            "claim": claim,
            "status": "Pendiente de revisión",
            "confidence": _cluster_confidence(cluster),
            "evidence": evidence,
            "review_note": _review_note(cluster),
        })

    return {
        "llm_used": False,
        "guardrail": GUARDRAIL,
        "findings": findings,
        "study_design_suggestion": _design_suggestion(parsed, findings),
        "limitations_note": _limitations_note(parsed, findings),
    }


def _summarise_cluster(cluster: list[tuple[str, str]], title: str) -> str:
    """Crea una afirmación que refleje el contenido del cluster sin inventar."""
    first = cluster[0][0]
    short = first if len(first) <= 220 else first[:217].rstrip() + "…"
    return f"Posible hallazgo del estudio «{title[:80]}»: {short}"


def _cluster_confidence(cluster: list[tuple[str, str]]) -> float:
    """Confianza técnica basada en número de evidencias y puntuación."""
    if not cluster:
        return 0.0
    avg = sum(_score_sentence(sentence) for sentence, _ in cluster) / len(cluster)
    return min(max(avg, 0.0), 0.95)


def _review_note(cluster: list[tuple[str, str]]) -> str:
    pages = sorted({page for _, page in cluster})
    if not pages:
        return "Sin páginas asociadas."
    return (
        "Verifique la frase en su contexto (página " + ", ".join(pages) + ")."
    )


# ---------------------------------------------------------------------------
# Sugerencias de diseño y limitaciones (heurísticas)
# ---------------------------------------------------------------------------
_DESIGN_KEYWORDS = {
    "rct": "ensayo controlado aleatorizado (RCT)",
    "randomi": "ensayo controlado aleatorizado (RCT)",
    "cohort": "estudio de cohorte",
    "case-control": "estudio de casos y controles",
    "cross-sectional": "estudio transversal",
    "transversal": "estudio transversal",
    "systematic review": "revisión sistemática",
    "meta-analysis": "metaanálisis",
    "metaanalysis": "metaanálisis",
    "qualitative": "estudio cualitativo",
    "pilot": "estudio piloto",
}


def _design_suggestion(parsed: ParsedPdf, findings: list[dict[str, Any]]) -> str:
    if not parsed.has_text:
        return "Sin texto extraíble: no es posible inferir el diseño del estudio."
    haystack = parsed.text.lower()
    for keyword, label in _DESIGN_KEYWORDS.items():
        if keyword in haystack:
            return f"Texto menciona indicadores compatibles con: {label}."
    return (
        "No se identificaron marcadores explícitos de diseño (RCT, cohorte, etc.). "
        "Revise la sección de métodos."
    )


_LIMITATION_KEYWORDS = {
    "limitation": "Limitaciones",
    "small sample": "Muestra reducida",
    "self-report": "Autoreporte",
    "self report": "Autoreporte",
    "short follow": "Seguimiento breve",
    "cross-sectional": "Diseño transversal (no causal)",
    "observational": "Diseño observacional (no causal)",
    "confound": "Posibles confusores",
}


def _limitations_note(parsed: ParsedPdf, findings: list[dict[str, Any]]) -> str:
    if not parsed.has_text:
        return "No se pudo extraer texto del PDF."
    haystack = parsed.text.lower()
    notes: list[str] = []
    for keyword, label in _LIMITATION_KEYWORDS.items():
        if keyword in haystack and label not in notes:
            notes.append(label)
    if not notes:
        return "Sin marcadores explícitos de limitaciones en el texto extraído."
    return "; ".join(notes)


# ---------------------------------------------------------------------------
# Integración opcional con la API
# ---------------------------------------------------------------------------
def _build_llm_prompt(parsed: ParsedPdf, title: str,
                      sentences: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Compone el prompt para la API compatible con OpenAI."""
    evidence_lines = [
        f"[Página {page}] {sentence}" for sentence, page in sentences[:30]
    ]
    system = textwrap.dedent(
        """
        Eres un asistente que organiza hallazgos de un PDF científico.
        Recibirás una lista numerada de oraciones literales extraídas del
        documento, cada una con su número de página.

        Devuelve EXCLUSIVAMENTE un JSON con la forma:

        {
          "findings": [
            {
              "claim": "afirmación breve (1 frase)",
              "evidence_indices": [1, 2]
            }
          ]
        }

        Reglas:
        - No inventes texto: cada cita debe corresponder a un índice
          recibido.
        - Máximo 5 hallazgos.
        - Mantén un tono neutral y descriptivo.
        - Si el texto no permite afirmar nada con seguridad, devuelve
          una lista vacía.
        """
    ).strip()
    user = (
        f"Título: {title}\n\n"
        "Oraciones literales (índice - página - texto):\n"
        + "\n".join(f"{i + 1}. [página {page}] {sentence}"
                    for i, (sentence, page) in enumerate(sentences[:30]))
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _call_llm(messages: list[dict[str, str]]) -> dict[str, Any] | None:
    """Llama a la API. Devuelve el JSON parseado o ``None`` ante cualquier error."""
    if not LLM_ENABLED:
        return None
    url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "response_format": {"type": "json_object"},
    }
    try:
        response = requests.post(
            url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        return None

    choices = body.get("choices") or []
    if not choices:
        return None
    content = choices[0].get("message", {}).get("content")
    if not content:
        return None
    try:
        return json.loads(content)
    except ValueError:
        # Algunos proveedores devuelven el JSON envuelto en fences.
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except ValueError:
            return None


def _validate_llm_findings(
    llm_payload: dict[str, Any],
    sentences: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Descarta cualquier afirmación que cite texto inexistente en el PDF."""
    valid: list[dict[str, Any]] = []
    raw_findings = llm_payload.get("findings") or []
    sentence_map = {idx + 1: (sentence, page)
                    for idx, (sentence, page) in enumerate(sentences[:30])}
    for item in raw_findings[:5]:
        claim = (item.get("claim") or "").strip()
        indices = item.get("evidence_indices") or []
        if not claim or not isinstance(indices, list):
            continue
        evidence: list[dict[str, str]] = []
        for raw_index in indices:
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            sentence, page = sentence_map.get(index, (None, None))
            if not sentence:
                continue
            evidence.append({"quote": _truncate(sentence, 320), "page": page})
        if not evidence:
            continue
        valid.append({
            "claim": claim,
            "status": "Pendiente de revisión",
            "confidence": 0.7,
            "evidence": evidence,
            "review_note": "Validar texto y contexto antes de utilizar esta afirmación.",
        })
    return valid


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def extract_reviewable_findings(
    parsed: ParsedPdf | None,
    title: str,
) -> dict[str, Any] | None:
    """Devuelve hallazgos con citas literales. ``None`` si no hay PDF legible."""
    if parsed is None or not parsed.has_text:
        return None

    sentences = _sentence_page_map(parsed)
    if not sentences:
        return {
            "llm_used": False,
            "guardrail": GUARDRAIL,
            "findings": [],
            "study_design_suggestion": _design_suggestion(parsed, []),
            "limitations_note": _limitations_note(parsed, []),
        }

    local_payload = _local_findings(parsed, title)

    if LLM_ENABLED:
        messages = _build_llm_prompt(parsed, title, sentences)
        llm_payload = _call_llm(messages)
        if llm_payload is not None:
            validated = _validate_llm_findings(llm_payload, sentences)
            if validated:
                return {
                    "llm_used": True,
                    "guardrail": GUARDRAIL,
                    "findings": validated,
                    "study_design_suggestion": local_payload["study_design_suggestion"],
                    "limitations_note": local_payload["limitations_note"],
                }
        # Si la API falla o devuelve citas inválidas, caemos al modo local.
    return local_payload
