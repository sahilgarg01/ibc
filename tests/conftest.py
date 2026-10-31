import pytest
import copy
import json
from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_sqlite(monkeypatch):
    # Tests must never modify the local PostgreSQL database from .env.
    monkeypatch.setenv("DATABASE_URL", "")


@pytest.fixture(autouse=True)
def mocked_pdf_extraction(request, monkeypatch):
    """Existing API tests use saved sample data, never a paid API call."""
    if request.node.path.name == "test_codex_extraction.py":
        return
    from backend.models import CaseRecord
    source = Path(__file__).resolve().parents[1] / "samples/starter-record.json"
    record = CaseRecord.model_validate_json(source.read_text().replace("SAMPLE_DOCUMENT", "SOURCE_DOCUMENT")).model_dump(mode="json")
    record["plans"] = [{"applicant": "Ashdan Properties Private Limited", "amount": "1452600000.00", "admitted": None, "document_id": "SOURCE_DOCUMENT", "page": 9}]
    result = {
        "provider": "codex_cli", "record": record, "subject": "Indo Global resolution order",
        "warnings": [], "pages": [{"page": n, "text": "Secured financial creditor evidence. " * 10 + " U72900PN2005PTC021732", "tables": []} for n in range(1, 24)],
        "case_facts": {"evidence": [], "warnings": [], "missing_fields": []},
    }
    def extract(content):
        if not content.startswith(b"%PDF-"):
            raise ValueError("Upload a valid PDF")
        return copy.deepcopy(result)
    for module in ("backend.main", "backend.url_import", "backend.automatic_orders"):
        monkeypatch.setattr(f"{module}.extract_pdf", extract)
