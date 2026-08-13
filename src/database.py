"""Capa de persistencia local en SQLite.

Almacena dos entidades:

* ``documents`` — expediente del estudio analizado. Se identifica por una
  huella (``fingerprint``) calculada a partir del PDF o del identificador
  externo, lo que permite reanalizar un mismo documento sin duplicar filas.
* ``human_decisions`` — registro auditable de las decisiones tomadas por
  una persona revisora. Cada entrada exige nombre y justificación; nunca
  se permite que el sistema cree una decisión por sí solo.

La ruta de la base de datos se toma de :mod:`src.config`. La base no se
distribuye con el repositorio.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.config import DB_PATH, HUMAN_DECISION_VALUES


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    """Abre una conexión SQLite con ``row_factory`` y claves foráneas activas."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Esquema
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    doi TEXT,
    source_url TEXT,
    filename TEXT,
    abstract TEXT,
    metadata_json TEXT,
    alerts_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reviewer_name TEXT NOT NULL,
    topic TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_decisions_document ON human_decisions (document_id);
CREATE INDEX IF NOT EXISTS idx_documents_doi ON documents (doi);
"""


def initialise_database() -> None:
    """Crea las tablas si no existen. Es idempotente y rápida."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_decision_payload(
    decision: str,
    reviewer_name: str | None,
    topic: str | None,
    rationale: str | None,
) -> tuple[str, str, str]:
    """Aplica las reglas mínimas: nombre, decisión válida, justificación no vacía."""
    if decision not in HUMAN_DECISION_VALUES:
        raise ValueError(
            "La decisión humana debe ser una de: " + ", ".join(HUMAN_DECISION_VALUES)
        )
    name = (reviewer_name or "").strip()
    if not name:
        raise ValueError("Debe registrar el nombre o identificador de la persona revisora.")
    topic_value = (topic or "").strip() or "Sin clasificar"
    justification = (rationale or "").strip()
    if not justification:
        raise ValueError("Debe registrar una justificación para la decisión humana.")
    return name, topic_value, justification


# ---------------------------------------------------------------------------
# Operaciones de documentos
# ---------------------------------------------------------------------------
def upsert_document(
    fingerprint: str,
    title: str,
    doi: str | None,
    source_url: str | None,
    filename: str | None,
    abstract: str | None,
    metadata: dict[str, Any],
    alerts: list[dict[str, Any]],
) -> int:
    """Inserta o actualiza un documento por ``fingerprint``. Devuelve su ``id``."""
    now = _utcnow()
    payload_meta = json.dumps(metadata, ensure_ascii=False, default=str)
    payload_alerts = json.dumps(alerts, ensure_ascii=False)

    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM documents WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE documents
                   SET title = ?,
                       doi = ?,
                       source_url = ?,
                       filename = ?,
                       abstract = ?,
                       metadata_json = ?,
                       alerts_json = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    title,
                    doi,
                    source_url,
                    filename,
                    abstract,
                    payload_meta,
                    payload_alerts,
                    now,
                    existing["id"],
                ),
            )
            conn.commit()
            return int(existing["id"])

        cursor = conn.execute(
            """
            INSERT INTO documents
                (fingerprint, title, doi, source_url, filename, abstract,
                 metadata_json, alerts_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                title,
                doi,
                source_url,
                filename,
                abstract,
                payload_meta,
                payload_alerts,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Decisiones humanas
# ---------------------------------------------------------------------------
def add_human_decision(
    document_id: int,
    decision: str,
    reviewer_name: str,
    topic: str,
    rationale: str,
) -> int:
    """Registra una decisión humana. Falla con ``ValueError`` si el formulario está incompleto."""
    name, topic_value, justification = _validate_decision_payload(
        decision, reviewer_name, topic, rationale
    )

    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if not exists:
            raise ValueError("El documento analizado ya no está disponible en el historial.")

        cursor = conn.execute(
            """
            INSERT INTO human_decisions
                (document_id, decision, reviewer_name, topic, rationale, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (document_id, decision, name, topic_value, justification, _utcnow()),
        )
        conn.commit()
        return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------
def get_review_history() -> list[dict[str, Any]]:
    """Devuelve todas las decisiones humanas con los datos clave del documento asociado."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.id            AS decision_id,
                   d.created_at    AS created_at,
                   d.decision      AS decision,
                   d.reviewer_name AS reviewer_name,
                   d.topic         AS topic,
                   d.rationale     AS rationale,
                   doc.title       AS title,
                   doc.doi         AS doi,
                   doc.filename    AS filename
              FROM human_decisions AS d
              JOIN documents       AS doc ON doc.id = d.document_id
             ORDER BY d.created_at DESC, d.id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_document_metadata(document_id: int) -> dict[str, Any] | None:
    """Devuelve metadatos y alertas almacenados para un documento, o ``None`` si no existe."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT metadata_json, alerts_json FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "alerts": json.loads(row["alerts_json"] or "[]"),
    }
