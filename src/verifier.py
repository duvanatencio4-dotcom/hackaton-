"""Módulo de verificación y contraste de evidencia científica con Google Gemini y Google Search Grounding.

Este módulo toma las afirmaciones o hallazgos del documento analizado y consulta
en tiempo real la API de Google Gemini con la herramienta Google Search habilitada,
contrastando las afirmaciones contra el consenso científico y fuentes públicas confiables.
"""
from __future__ import annotations

import json
import re
from typing import Any

import requests

from src.config import (
    GEMINI_API_KEY,
    GEMINI_ENABLED,
    GEMINI_ENDPOINT,
    GEMINI_MODEL,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from src.pdf_tools import ParsedPdf

VERIFICATION_GUARDRAIL = (
    "Esta verificación se generó mediante contraste en tiempo real con Google Search Grounding. "
    "Los veredictos y resúmenes son orientativos y deben ser validados por la persona revisora "
    "consultando las fuentes primarias enlazadas."
)


def _extract_json_from_response(text: str) -> dict[str, Any] | None:
    """Extrae un objeto JSON válido del texto devuelto por el modelo."""
    if not text:
        return None
    
    # Intentar parseo directo
    cleaned = text.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Intentar extraer bloques ```json ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Intentar extraer desde la primera llave '{' hasta la última '}'
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(text[first_brace : last_brace + 1])
        except json.JSONDecodeError:
            pass

    return None


def _build_verification_prompt(claims: list[str], title: str) -> str:
    claims_text = "\n".join(f"{i+1}. {claim}" for i, claim in enumerate(claims))
    return f"""Eres un asistente científico de fact-checking y revisión sistemática.
Tu tarea es contrastar las afirmaciones de un estudio titulado: "{title}" contra la literatura científica y fuentes confiables en Internet utilizando búsqueda web en tiempo real.

Afirmaciones a verificar:
{claims_text}

Instrucciones:
1. Realiza búsquedas sobre cada afirmación para encontrar evidencia científica, revisiones sistemáticas, metaanálisis o artículos indexados en PubMed/Nature/SciELO/Cochrane.
2. Determina un veredicto para cada afirmación entre:
   - "RESPALDADO": Si la evidencia científica actual y el consenso la corroboran.
   - "EN DEBATE": Si existe controversia, resultados mixtos o la evidencia es preliminar/limitada.
   - "CONTRADICHO": Si la literatura de calidad o metaanálisis contradicen la afirmación.
   - "NO CONCLUYENTE": Si no hay suficientes estudios confiables para validar la afirmación.
3. Proporciona una explicación concisa y objetiva con el respaldo y las limitaciones encontradas.

IMPORTANTE: Responde ÚNICAMENTE con un objeto JSON válido con la siguiente estructura exacta:
{{
  "overview": "Resumen ejecutivo global del contraste del estudio con el estado del arte en internet (2 a 4 oraciones).",
  "verifications": [
    {{
      "claim": "Texto de la afirmación analizada",
      "verdict": "RESPALDADO" | "EN DEBATE" | "CONTRADICHO" | "NO CONCLUYENTE",
      "summary": "Explicación del contraste con la literatura externa y consenso científico.",
      "supporting_points": "Evidencias o estudios a favor encontrados.",
      "caveats_or_conflict": "Discrepancias, limitaciones metodológicas o evidencia contraria."
    }}
  ]
}}
"""


def verify_claims_with_gemini(
    claims: list[str],
    title: str = "",
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Contrasta afirmaciones con Internet usando Gemini y Google Search Grounding."""
    effective_key = api_key or GEMINI_API_KEY
    effective_model = model or GEMINI_MODEL

    if not effective_key:
        return {
            "success": False,
            "error": "No se encontró GEMINI_API_KEY. Configure su clave en .streamlit/secrets.toml o .env.",
            "overview": None,
            "verifications": [],
            "sources": [],
            "search_queries": [],
            "guardrail": VERIFICATION_GUARDRAIL,
        }

    if not claims:
        return {
            "success": False,
            "error": "No se proporcionaron afirmaciones para verificar.",
            "overview": None,
            "verifications": [],
            "sources": [],
            "search_queries": [],
            "guardrail": VERIFICATION_GUARDRAIL,
        }

    # Limitar a 5 afirmaciones principales para optimizar tiempos y precisión
    selected_claims = [c.strip() for c in claims if c.strip()][:5]
    prompt = _build_verification_prompt(selected_claims, title or "Estudio científico")

    url = GEMINI_ENDPOINT.format(model=effective_model)
    params = {"key": effective_key}
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "tools": [
            {"google_search": {}}
        ],
        "generationConfig": {
            "temperature": 0.1,
        },
    }

    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            url,
            params=params,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT * 2,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        error_detail = ""
        if hasattr(exc, "response") and exc.response is not None:
            try:
                err_json = exc.response.json()
                error_detail = f": {err_json.get('error', {}).get('message', '')}"
            except Exception:
                error_detail = f" (HTTP {exc.response.status_code})"
        return {
            "success": False,
            "error": f"Error al conectar con la API de Gemini ({exc.__class__.__name__}){error_detail}.",
            "overview": None,
            "verifications": [],
            "sources": [],
            "search_queries": [],
            "guardrail": VERIFICATION_GUARDRAIL,
        }

    candidates = data.get("candidates", [])
    if not candidates:
        return {
            "success": False,
            "error": "La API de Gemini no devolvió candidatos válidos en la respuesta.",
            "overview": None,
            "verifications": [],
            "sources": [],
            "search_queries": [],
            "guardrail": VERIFICATION_GUARDRAIL,
        }

    candidate = candidates[0]
    content_parts = candidate.get("content", {}).get("parts", [])
    response_text = "".join(part.get("text", "") for part in content_parts)

    # Extraer metadatos de Google Search Grounding
    grounding_meta = candidate.get("groundingMetadata", {})
    search_queries = grounding_meta.get("webSearchQueries", [])
    
    # Extraer fuentes web
    sources: list[dict[str, str]] = []
    seen_urls = set()
    for chunk in grounding_meta.get("groundingChunks", []):
        web = chunk.get("web", {})
        uri = web.get("uri")
        title_source = web.get("title") or uri
        if uri and uri not in seen_urls:
            seen_urls.add(uri)
            sources.append({"url": uri, "title": title_source})

    # Parsear JSON de veredicto
    parsed_json = _extract_json_from_response(response_text)
    
    if not parsed_json:
        # Si no estructuró en JSON, devolvemos el texto como overview general
        return {
            "success": True,
            "overview": response_text.strip(),
            "verifications": [],
            "sources": sources,
            "search_queries": search_queries,
            "guardrail": VERIFICATION_GUARDRAIL,
        }

    verifications = parsed_json.get("verifications", [])
    overview = parsed_json.get("overview", "")

    return {
        "success": True,
        "overview": overview,
        "verifications": verifications,
        "sources": sources,
        "search_queries": search_queries,
        "guardrail": VERIFICATION_GUARDRAIL,
    }
