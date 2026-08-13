"""Tests para ``src.database``.

Cada test usa una base de datos temporal mediante ``tmp_path`` para no
tocar la base real del usuario. Se valida:

* El esquema se crea correctamente.
* ``upsert_document`` inserta y actualiza por ``fingerprint``.
* ``add_human_decision`` exige nombre y justificación (lanza ``ValueError``).
* ``get_review_history`` devuelve un registro ligado al documento.
"""
from __future__ import annotations

import json
from typing import Iterator

import pytest

import src.database as db


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Redirige la BD a un archivo temporal antes de cada test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    # Forzar recarga del esquema contra la nueva ruta
    db.initialise_database()
    yield
    # Limpieza: el archivo temporal se borra con tmp_path


# ---------------------------------------------------------------------------
# Esquema
# ---------------------------------------------------------------------------
def test_initialise_creates_tables(isolated_db: None) -> None:
    import sqlite3
    with sqlite3.connect(db.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {row[0] for row in rows}
    assert "documents" in names
    assert "human_decisions" in names


def test_initialise_is_idempotent(isolated_db: None) -> None:
    # Segunda llamada no debe fallar
    db.initialise_database()
    db.initialise_database()


# ---------------------------------------------------------------------------
# Documentos
# ---------------------------------------------------------------------------
def test_upsert_inserts_new_document(isolated_db: None) -> None:
    doc_id = db.upsert_document(
        fingerprint="abc",
        title="Estudio X",
        doi="10.1234/test",
        source_url=None,
        filename="x.pdf",
        abstract="Resumen",
        metadata={"k": 1},
        alerts=[{"level": "info", "label": "ok", "detail": "ok", "source": "test"}],
    )
    assert isinstance(doc_id, int) and doc_id > 0


def test_upsert_updates_existing_document(isolated_db: None) -> None:
    doc_id_1 = db.upsert_document(
        fingerprint="f1", title="T1", doi=None, source_url=None,
        filename=None, abstract=None, metadata={}, alerts=[],
    )
    doc_id_2 = db.upsert_document(
        fingerprint="f1", title="T1 actualizado", doi="10.9999/up",
        source_url=None, filename=None, abstract=None,
        metadata={"v": 2}, alerts=[],
    )
    assert doc_id_1 == doc_id_2

    meta = db.get_document_metadata(doc_id_1)
    assert meta is not None
    assert meta["metadata"] == {"v": 2}
    assert "update-to" not in json.dumps(meta["metadata"])  # sanity


# ---------------------------------------------------------------------------
# Decisiones humanas
# ---------------------------------------------------------------------------
def test_add_human_decision_stores_record(isolated_db: None) -> None:
    doc_id = db.upsert_document(
        fingerprint="f2", title="Estudio Y", doi=None, source_url=None,
        filename=None, abstract=None, metadata={}, alerts=[],
    )
    record_id = db.add_human_decision(
        document_id=doc_id,
        decision="Aprobar",
        reviewer_name="Ana",
        topic="Sueño",
        rationale="Coincide con la nota editorial",
    )
    assert record_id > 0
    history = db.get_review_history()
    assert len(history) == 1
    row = history[0]
    assert row["reviewer_name"] == "Ana"
    assert row["topic"] == "Sueño"
    assert row["decision"] == "Aprobar"
    assert row["title"] == "Estudio Y"
    assert row["rationale"] == "Coincide con la nota editorial"


def test_add_human_decision_rejects_blank_name(isolated_db: None) -> None:
    doc_id = db.upsert_document(
        fingerprint="f3", title="X", doi=None, source_url=None,
        filename=None, abstract=None, metadata={}, alerts=[],
    )
    with pytest.raises(ValueError, match="revisora"):
        db.add_human_decision(
            document_id=doc_id,
            decision="Aprobar",
            reviewer_name="   ",
            topic="Sueño",
            rationale="Texto",
        )


def test_add_human_decision_rejects_blank_rationale(isolated_db: None) -> None:
    doc_id = db.upsert_document(
        fingerprint="f4", title="X", doi=None, source_url=None,
        filename=None, abstract=None, metadata={}, alerts=[],
    )
    with pytest.raises(ValueError, match="justificaci"):
        db.add_human_decision(
            document_id=doc_id,
            decision="Aprobar",
            reviewer_name="Ana",
            topic="Sueño",
            rationale="",
        )


def test_add_human_decision_rejects_invalid_decision(isolated_db: None) -> None:
    doc_id = db.upsert_document(
        fingerprint="f5", title="X", doi=None, source_url=None,
        filename=None, abstract=None, metadata={}, alerts=[],
    )
    with pytest.raises(ValueError):
        db.add_human_decision(
            document_id=doc_id,
            decision="Descartar",  # no está en HUMAN_DECISION_VALUES
            reviewer_name="Ana",
            topic="Sueño",
            rationale="Texto",
        )


def test_add_human_decision_fails_for_missing_document(isolated_db: None) -> None:
    with pytest.raises(ValueError):
        db.add_human_decision(
            document_id=9999,
            decision="Aprobar",
            reviewer_name="Ana",
            topic="Sueño",
            rationale="Texto",
        )


def test_history_orders_newest_first(isolated_db: None) -> None:
    doc_id = db.upsert_document(
        fingerprint="f6", title="X", doi=None, source_url=None,
        filename=None, abstract=None, metadata={}, alerts=[],
    )
    db.add_human_decision(
        document_id=doc_id, decision="Pendiente", reviewer_name="Ana",
        topic="Sueño", rationale="primero",
    )
    db.add_human_decision(
        document_id=doc_id, decision="Aprobar", reviewer_name="Ana",
        topic="Sueño", rationale="segundo",
    )
    history = db.get_review_history()
    assert history[0]["rationale"] == "segundo"
    assert history[1]["rationale"] == "primero"
