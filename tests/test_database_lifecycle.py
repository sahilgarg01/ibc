"""Regression checks for transaction and lifecycle boundaries after the refactor."""
import sqlite3
from unittest.mock import MagicMock

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.db import Database
from backend.main import ROOT, create_app
from backend.models import CaseRecord


def test_session_rolls_back_and_initialization_preserves_existing_records(tmp_path):
    database = Database(tmp_path, database_url="")
    database.initialize()
    record = CaseRecord().model_dump_json()

    with pytest.raises(RuntimeError, match="abort transaction"):
        with database.session() as session:
            session.insert_case("rolled-back", record, Database.timestamp())
            raise RuntimeError("abort transaction")

    with database.session() as session:
        assert session.all_cases() == []
        session.insert_case("retained", record, Database.timestamp())

    database.initialize()
    with database.session() as session:
        assert [row["id"] for row in session.all_cases()] == ["retained"]


def test_ingestion_failure_rolls_back_records_and_removes_pdf(tmp_path, monkeypatch):
    def fail_projection(self, case_id, record):
        raise RuntimeError("projection failed")

    monkeypatch.setattr(Database, "project", fail_projection)
    pdf = (ROOT / "samples/indo-global-resolution-order.pdf").read_bytes()

    with TestClient(create_app(tmp_path)) as client:
        response = client.post(
            "/api/documents", files={"file": ("order.pdf", pdf, "application/pdf")}
        )
        assert response.status_code == 500
        assert client.get("/api/cases").json() == []
        assert client.get("/api/catalogue").json()["total"] == 0
        job = client.get("/api/imports/" + response.json()["import_job_id"]).json()
        assert job["status"] == "failed"
        assert job["result"]["failed"] == 1

    assert list((tmp_path / "documents").glob("*.pdf")) == []


@pytest.mark.parametrize("error", [sqlite3.OperationalError, psycopg.OperationalError])
def test_database_errors_keep_the_existing_http_response(tmp_path, monkeypatch, error):
    def fail_read(self):
        raise error("private connection details")

    with TestClient(create_app(tmp_path)) as client:
        monkeypatch.setattr(Database, "all_cases", fail_read)
        response = client.get("/api/cases")
        assert response.status_code == 503
        assert "Database operation failed" in response.json()["detail"]
        assert "private connection details" not in response.text


def test_postgres_session_preserves_parameter_and_transaction_adaptation(tmp_path, monkeypatch):
    handle = MagicMock()
    handle.__enter__.return_value = handle
    connect = MagicMock(return_value=handle)
    monkeypatch.setattr("backend.db.psycopg.connect", connect)

    with Database(tmp_path, "postgresql://test.invalid/isolated").session() as session:
        session.begin()
        handle.execute.assert_not_called()
        session.execute("INSERT OR IGNORE INTO example VALUES(?)", ("value",))
        handle.execute.assert_called_once_with(
            "INSERT INTO example VALUES(%s) ON CONFLICT DO NOTHING", ("value",)
        )

    handle.__exit__.assert_called_once_with(None, None, None)
    assert connect.call_args.kwargs["row_factory"] is Database.flex_row


def test_invalid_order_filter_still_returns_validation_error(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/api/orders", params={"adverse": "invalid"})
        assert response.status_code == 422
        assert response.json()["detail"] == "Invalid adverse filter"
