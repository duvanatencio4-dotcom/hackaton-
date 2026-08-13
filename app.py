"""MVP local: Asistente de Revisión Científica para Doma.

Ejecución: streamlit run app.py
"""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

from src.config import (
    APP_NAME,
    GEMINI_API_KEY,
    GEMINI_ENABLED,
    GEMINI_MODEL,
    HUMAN_DECISION_VALUES,
    HUMAN_REVIEW_NOTICE,
    LLM_ENABLED,
    MAX_UPLOAD_MB,
    REQUEST_TIMEOUT,
    TOPIC_PRESETS,
    USER_AGENT,
)
from src.database import add_human_decision, get_review_history, initialise_database, upsert_document
from src.extractor import extract_reviewable_findings
from src.metadata_services import canonical_doi, inspect_study
from src.pdf_tools import DOI_PATTERN, ParsedPdf, extract_title_heuristic, parse_pdf
from src.verifier import VERIFICATION_GUARDRAIL, verify_claims_with_gemini

st.set_page_config(page_title=APP_NAME, page_icon="🧬", layout="wide", initial_sidebar_state="expanded")


@st.cache_resource
def setup_database() -> bool:
    initialise_database()
    return True


setup_database()


def init_state() -> None:
    defaults: dict[str, Any] = {
        "analysis": None,
        "document_id": None,
        "pdf_bytes": None,
        "pdf_name": None,
        "verification": None,
        "last_verified_doc_id": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_identifier(value: str) -> tuple[str | None, str | None]:
    value = value.strip()
    doi = canonical_doi(value)
    pmid_match = re.search(r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|PMID\s*:?\s*)(\d{5,9})", value, re.I)
    return doi, pmid_match.group(1) if pmid_match else None


def fetch_pdf(url: str) -> tuple[bytes | None, str | None, str | None]:
    """Descarga solo PDFs, con límite de tamaño; otros enlaces se tratan como identificadores."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None, None, "El enlace debe comenzar con http:// o https://."
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/pdf"},
            stream=True,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" not in content_type and not response.url.lower().split("?")[0].endswith(".pdf"):
            return None, None, "El enlace no parece apuntar a un PDF. Se consultarán metadatos si contiene DOI o PMID."
        max_bytes = MAX_UPLOAD_MB * 1024 * 1024
        payload = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            payload.extend(chunk)
            if len(payload) > max_bytes:
                return None, None, f"El PDF supera el límite configurado de {MAX_UPLOAD_MB} MB."
        filename = response.url.split("/")[-1].split("?")[0] or "estudio.pdf"
        return bytes(payload), filename, None
    except requests.RequestException as exc:
        return None, None, f"No se pudo descargar el PDF: {exc.__class__.__name__}."


def analyze_input(uploaded_file: Any, supplied_link: str) -> tuple[dict[str, Any] | None, str | None]:
    """Ejecuta detector y extractor; no cambia nunca el estado editorial del estudio."""
    pdf_bytes: bytes | None = None
    filename: str | None = None
    source_url = supplied_link.strip() or None
    doi, pmid = parse_identifier(supplied_link)

    if uploaded_file is not None:
        if uploaded_file.size > MAX_UPLOAD_MB * 1024 * 1024:
            return None, f"El archivo supera el límite de {MAX_UPLOAD_MB} MB."
        pdf_bytes = uploaded_file.getvalue()
        filename = uploaded_file.name
    elif supplied_link.strip().lower().endswith(".pdf"):
        pdf_bytes, filename, download_error = fetch_pdf(supplied_link.strip())
        if download_error and not pdf_bytes:
            return None, download_error

    parsed: ParsedPdf | None = None
    local_title: str | None = None
    if pdf_bytes:
        try:
            parsed = parse_pdf(pdf_bytes, filename or "estudio.pdf")
        except Exception as exc:  # El mensaje técnico se reserva a consola; la interfaz evita detalles sensibles.
            return None, f"No se pudo procesar el PDF ({exc.__class__.__name__}). Compruebe que el archivo no esté protegido o dañado."
        doi = doi or parsed.doi
        pmid = pmid or parsed.pmid
        local_title = extract_title_heuristic(parsed.text)

    if not parsed and not doi and not pmid:
        return None, "Suba un PDF o proporcione un enlace que contenga un DOI, un PMID o una URL directa a PDF."

    pdf_text_status = bool(parsed.text) if parsed is not None else None
    inspection = inspect_study(doi, pmid, local_title, pdf_text_status)
    remote_title = (
        (inspection.get("crossref") or {}).get("title")
        or (inspection.get("pubmed") or {}).get("title")
    )
    title = local_title or remote_title or "Documento sin título confirmado"
    findings = extract_reviewable_findings(parsed, title) if parsed and parsed.text else None
    fingerprint = parsed.fingerprint if parsed else sha256_text(inspection.get("doi") or pmid or source_url or title)

    metadata = {
        "inspection": inspection,
        "extractor": findings,
        "local_title_heuristic": local_title,
        "document_text_available": bool(parsed and parsed.text),
    }
    document_id = upsert_document(
        fingerprint=fingerprint,
        title=title,
        doi=inspection.get("doi"),
        source_url=source_url,
        filename=filename,
        abstract=(inspection.get("pubmed") or {}).get("abstract"),
        metadata=metadata,
        alerts=inspection["alerts"],
    )
    return {
        "document_id": document_id,
        "title": title,
        "doi": inspection.get("doi"),
        "pmid": (inspection.get("pubmed") or {}).get("pmid") or pmid,
        "source_url": source_url,
        "filename": filename,
        "parsed": parsed,
        "inspection": inspection,
        "findings": findings,
    }, None


def level_label(level: str) -> str:
    return {"alta": "ALTA", "media": "MEDIA", "info": "INFORMACIÓN"}.get(level.lower(), level.upper())


def render_alerts(alerts: list[dict[str, str]]) -> None:
    st.subheader("Alertas y verificaciones sugeridas")
    for alert in alerts:
        title = f"{level_label(alert['level'])} · {alert['label']}"
        if alert["level"] == "alta":
            st.error(f"**{title}**\n\n{alert['detail']}\n\nFuente: {alert['source']}")
        elif alert["level"] == "media":
            st.warning(f"**{title}**\n\n{alert['detail']}\n\nFuente: {alert['source']}")
        else:
            st.info(f"**{title}**\n\n{alert['detail']}\n\nFuente: {alert['source']}")


def render_metadata(inspection: dict[str, Any]) -> None:
    with st.expander("Metadatos contrastados", expanded=False):
        crossref = inspection.get("crossref") or {}
        pubmed = inspection.get("pubmed") or {}
        openalex = inspection.get("openalex") or {}
        rows = [
            ("DOI", inspection.get("doi") or "No confirmado"),
            ("Título (Crossref)", crossref.get("title") or "No disponible"),
            ("Revista", crossref.get("journal") or "No disponible"),
            ("Fecha", crossref.get("published") or "No disponible"),
            ("PMID", pubmed.get("pmid") or "No localizado"),
            ("OpenAlex: retractado", str(openalex.get("is_retracted")) if openalex else "No localizado"),
            ("Consulta", inspection.get("checked_at", "")),
        ]
        st.dataframe(pd.DataFrame(rows, columns=["Campo", "Valor"]), hide_index=True, use_container_width=True)
        links = []
        for name, record in (("Crossref", crossref), ("PubMed", pubmed), ("OpenAlex", openalex)):
            url = record.get("url") or record.get("landing_page_url")
            if url:
                links.append(f"[{name}]({url})")
        if links:
            st.caption("Abrir fuente primaria: " + " · ".join(links))


def render_document_viewer(analysis: dict[str, Any]) -> None:
    parsed: ParsedPdf | None = analysis.get("parsed")
    if not parsed:
        return
    st.subheader("Documento fuente")
    if st.session_state.pdf_bytes:
        encoded = base64.b64encode(st.session_state.pdf_bytes).decode("ascii")
        components.html(
            f'<iframe src="data:application/pdf;base64,{encoded}" width="100%" height="560" title="Documento fuente"></iframe>',
            height=570,
            scrolling=False,
        )
        st.download_button(
            "Descargar PDF fuente",
            data=st.session_state.pdf_bytes,
            file_name=analysis.get("filename") or "estudio.pdf",
            mime="application/pdf",
        )
    with st.expander("Texto extraído para inspección", expanded=False):
        st.text_area("Texto", parsed.text[:25000] or "No se extrajo texto.", height=350, disabled=True, label_visibility="collapsed")


def render_findings(findings: dict[str, Any] | None) -> None:
    st.subheader("Extractor de citas y hallazgos")
    if not findings:
        st.warning("No hay texto extraíble para generar citas. Revise el PDF o aplique OCR antes de usar este módulo.")
        return
    if findings["llm_used"]:
        st.caption("Se usó la API configurada para organizar sugerencias; las citas se verificaron localmente contra el PDF.")
    else:
        st.caption("Modo sin síntesis por API: se muestran fragmentos literales para revisión manual.")
    st.info(findings["guardrail"])
    for position, finding in enumerate(findings["findings"], start=1):
        st.markdown(f"**Hallazgo sugerido {position}:** {finding['claim']}")
        col_status, col_conf = st.columns([2, 1])
        with col_status:
            st.caption(f"Estado: **{finding['status']}**")
        with col_conf:
            st.caption(f"Confianza técnica: {finding['confidence']:.0%}")
        for evidence in finding["evidence"]:
            st.markdown(f"> “{evidence['quote']}”\n> \n> **Cita literal verificada — página {evidence['page']}**")
        st.caption("Nota de revisión: " + finding["review_note"])
        st.divider()
    with st.expander("Orientaciones que requieren comprobación humana", expanded=False):
        st.write("Diseño sugerido: " + findings["study_design_suggestion"])
        st.write("Limitaciones: " + findings["limitations_note"])


def render_web_verification(analysis: dict[str, Any]) -> None:
    st.subheader("🌐 Fact-Checking y Contraste con Internet")
    st.markdown(
        "Este módulo utiliza la API de **Google Gemini** con **Google Search Grounding** para buscar en tiempo real "
        "en la web académica (PubMed, Nature, Cochrane, SciELO, etc.) y contrastar las afirmaciones del estudio."
    )

    if not GEMINI_ENABLED:
        st.warning(
            "⚠️ **API de Gemini no configurada.**\n\n"
            "Para activar la búsqueda y verificación en Internet, configure su clave en `.streamlit/secrets.toml` "
            "o en su archivo `.env`:\n\n"
            "```toml\n"
            "# .streamlit/secrets.toml\n"
            'GEMINI_API_KEY = "tu_clave_de_google_ai_studio"\n'
            "```"
        )
        return

    # Comprobar si ya existe verificación para este documento
    is_same_doc = st.session_state.last_verified_doc_id == analysis.get("document_id")
    verification_data = st.session_state.verification if is_same_doc else None

    # Extraer afirmaciones del extractor o del documento
    claims_to_check: list[str] = []
    findings = analysis.get("findings")
    if findings and findings.get("findings"):
        claims_to_check = [f["claim"] for f in findings["findings"]]
    elif analysis.get("parsed") and analysis["parsed"].text:
        # Extraer oraciones representativas si no hay findings
        raw_text = analysis["parsed"].text
        claims_to_check = [line.strip() for line in raw_text.splitlines() if len(line.strip()) > 50][:4]

    col_btn, col_info = st.columns([1, 2])
    with col_btn:
        run_btn = st.button("🔍 Contrastar con Internet", type="primary", use_container_width=True)
    with col_info:
        st.caption(f"Modelo: `{GEMINI_MODEL}` con búsqueda web activa.")

    if run_btn:
        if not claims_to_check:
            st.error("No se encontraron afirmaciones suficientes en el documento para contrastar.")
            return

        with st.spinner("Buscando en la web científica y contrastando afirmaciones con Gemini..."):
            res = verify_claims_with_gemini(claims_to_check, title=analysis.get("title", ""))
            if res.get("success"):
                st.session_state.verification = res
                st.session_state.last_verified_doc_id = analysis.get("document_id")
                verification_data = res
                st.success("¡Verificación y búsqueda web completada!")
            else:
                st.error(res.get("error", "Error desconocido durante la verificación."))

    if verification_data and verification_data.get("success"):
        st.info(f"🛡️ **Aviso de verificación:** {verification_data.get('guardrail', VERIFICATION_GUARDRAIL)}")

        if verification_data.get("overview"):
            st.markdown("### 📊 Resumen del contraste científico")
            st.markdown(f"> {verification_data['overview']}")

        # Búsquedas realizadas
        queries = verification_data.get("search_queries", [])
        if queries:
            with st.expander("Búsquedas web realizadas por el agente", expanded=False):
                st.write(", ".join([f"`{q}`" for q in queries]))

        # Verificaciones individuales
        verifications = verification_data.get("verifications", [])
        if verifications:
            st.markdown("### 📋 Evaluación de afirmaciones")
            for i, v in enumerate(verifications, start=1):
                verdict = str(v.get("verdict", "NO CONCLUYENTE")).upper()
                badge = "⚪"
                if "RESPALDADO" in verdict:
                    badge = "🟢"
                elif "DEBATE" in verdict or "PARCIAL" in verdict:
                    badge = "🟡"
                elif "CONTRADICHO" in verdict or "REFUTADO" in verdict:
                    badge = "🔴"

                with st.container():
                    st.markdown(f"#### {badge} Afirmación {i}: {verdict}")
                    st.markdown(f"**Afirmación:** *\"{v.get('claim', '')}\"*")
                    st.markdown(f"**Contraste:** {v.get('summary', '')}")
                    if v.get("supporting_points"):
                        st.markdown(f"- **Evidencia a favor:** {v['supporting_points']}")
                    if v.get("caveats_or_conflict"):
                        st.markdown(f"- **Discrepancias / Limitaciones:** {v['caveats_or_conflict']}")
                    st.divider()

        # Fuentes web encontradas por Grounding
        sources = verification_data.get("sources", [])
        if sources:
            st.markdown("### 🔗 Fuentes primarias y referencias encontradas en Internet")
            for s in sources:
                st.markdown(f"- [{s.get('title', s['url'])}]({s['url']})")
    elif not run_btn:
        st.caption("Haga clic en **'Contrastar con Internet'** para iniciar la búsqueda en vivo y evaluar las afirmaciones.")


def render_human_decision(analysis: dict[str, Any]) -> None:
    st.subheader("Decisión humana")
    st.warning("**Regla de oro:** el sistema no aprueba ni rechaza documentos. Solo una persona revisora puede registrar una decisión.")
    with st.form("human_decision_form", clear_on_submit=True):
        reviewer = st.text_input("Nombre o identificador de la persona revisora *")
        topic = st.selectbox("Clasificación temática *", TOPIC_PRESETS)
        decision = st.radio("Decisión humana *", HUMAN_DECISION_VALUES, horizontal=True, index=0)
        rationale = st.text_area("Justificación de la decisión *", placeholder="Explique la evidencia revisada, dudas o criterios aplicados.")
        submitted = st.form_submit_button("Registrar decisión humana")
    if submitted:
        try:
            record_id = add_human_decision(
                document_id=analysis["document_id"],
                decision=decision,
                reviewer_name=reviewer,
                topic=topic,
                rationale=rationale,
            )
            st.success(f"Decisión humana registrada en el historial (ID {record_id}).")
        except ValueError as exc:
            st.error(str(exc))


def render_history() -> None:
    st.subheader("Historial de decisiones humanas")
    history = get_review_history()
    if not history:
        st.info("Aún no se han registrado decisiones humanas.")
        return
    frame = pd.DataFrame(history)
    visible_columns = ["created_at", "title", "doi", "topic", "decision", "reviewer_name", "rationale"]
    st.dataframe(frame[visible_columns], hide_index=True, use_container_width=True)
    st.download_button(
        "Exportar historial como CSV",
        data=frame.to_csv(index=False).encode("utf-8-sig"),
        file_name="historial_revision_cientifica.csv",
        mime="text/csv",
    )


def render_about() -> None:
    st.subheader("Alcance y límites del MVP")
    st.markdown(
        "Este prototipo combina lectura local del PDF con consultas públicas de metadatos y búsqueda web asistida por IA. "
        "Crossref distribuye datos de retractaciones y actualizaciones (incluidos registros de Retraction Watch). [1] "
        "PubMed se consulta mediante las E-utilities de NCBI. [2] La verificación web utiliza Google Gemini con Grounding en tiempo real. "
        "Ninguna fuente, alerta o verificación automática equivale a un veredicto editorial definitivo."
    )
    st.markdown(
        "> **Principio operativo:** cada cita mostrada debe existir literalmente en el PDF y toda clasificación, "
        "aprobación o rechazo se registra como una decisión humana atribuible."
    )
    table = pd.DataFrame(
        [
            ("Detector", "Señales de retractación, corrección, inconsistencia o identificador incompleto.", "No certifica fiabilidad."),
            ("Extractor", "Fragmentos literales con página y hallazgos sugeridos.", "No sustituye la lectura ni inventa citas."),
            ("Fact-Checking Web", "Búsqueda en tiempo real con Google Search Grounding y contraste de hipótesis.", "No reemplaza el juicio crítico."),
            ("Panel", "Tema, decisión humana, justificación e historial local SQLite.", "No ejecuta decisiones automáticas."),
        ],
        columns=["Módulo", "Aporta", "No hace"],
    )
    st.table(table)
    st.markdown("### Referencias\n\n[1] [Crossref — Retraction Watch](https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/)\n\n[2] [NCBI — E-utilities](https://www.ncbi.nlm.nih.gov/home/develop/api/)")


def main() -> None:
    init_state()
    st.markdown(
        """
        <style>
        .block-container {max-width: 1400px; padding-top: 2.3rem;}
        [data-testid="stMetric"] {background: #f4f8f8; border: 1px solid #d9e6e5; padding: .6rem; border-radius: .55rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("🧬 Asistente de Revisión Científica")
    st.caption("MVP para Doma · evidencias trazables, alertas explicables, fact-checking web y decisión final humana.")
    st.warning(HUMAN_REVIEW_NOTICE)

    with st.sidebar:
        st.header("Entrada del estudio")
        uploaded = st.file_uploader("Subir PDF", type=["pdf"], help=f"Máximo {MAX_UPLOAD_MB} MB.")
        link = st.text_input("pega DOI, PMID o enlace a PDF", placeholder="10.1000/ejemplo o https://…")
        
        if GEMINI_ENABLED:
            st.success("🌐 Verificación web (Gemini): configurada")
        elif LLM_ENABLED:
            st.success("Síntesis asistida por API: configurada")
        else:
            st.info("🌐 Fact-Checking web: clave no detectada. Configure GEMINI_API_KEY en .streamlit/secrets.toml o .env")
            
        process = st.button("Analizar documento", type="primary", use_container_width=True)
        st.divider()
        st.caption("El procesamiento se realiza bajo demanda. La aplicación no toma decisiones editoriales automáticamente.")

    if process:
        with st.spinner("Leyendo el documento y contrastando metadatos públicos…"):
            analysis, error = analyze_input(uploaded, link)
        if error:
            st.error(error)
        else:
            st.session_state.analysis = analysis
            st.session_state.document_id = analysis["document_id"]
            st.session_state.pdf_bytes = uploaded.getvalue() if uploaded is not None else None
            # Para PDF remoto, se recarga de forma controlada solo para el visor.
            if analysis.get("parsed") and st.session_state.pdf_bytes is None and link.strip().lower().endswith(".pdf"):
                downloaded, _, _ = fetch_pdf(link.strip())
                st.session_state.pdf_bytes = downloaded
            st.session_state.pdf_name = analysis.get("filename")
            st.session_state.verification = None
            st.session_state.last_verified_doc_id = None
            st.success("Análisis preparado. Revise las evidencias antes de registrar cualquier decisión.")

    analysis = st.session_state.analysis
    if not analysis:
        st.info("Comience subiendo un PDF o pegando un DOI, PMID o enlace directo a PDF.")
        render_about()
        return

    st.divider()
    st.subheader(analysis["title"])
    metric_a, metric_b, metric_c = st.columns(3)
    metric_a.metric("DOI", analysis.get("doi") or "No confirmado")
    metric_b.metric("PMID", analysis.get("pmid") or "No localizado")
    metric_c.metric("Alertas", len(analysis["inspection"]["alerts"]))

    tabs = st.tabs(["Detector", "Extractor", "🌐 Fact-Checking Web", "Panel humano", "Historial", "Acerca del MVP"])
    with tabs[0]:
        render_alerts(analysis["inspection"]["alerts"])
        render_metadata(analysis["inspection"])
        render_document_viewer(analysis)
    with tabs[1]:
        render_findings(analysis.get("findings"))
    with tabs[2]:
        render_web_verification(analysis)
    with tabs[3]:
        render_human_decision(analysis)
    with tabs[4]:
        render_history()
    with tabs[5]:
        render_about()


if __name__ == "__main__":
    main()
