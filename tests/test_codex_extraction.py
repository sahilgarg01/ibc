import copy
import json

import subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from backend import codex_extraction as extraction
from backend.db import connection
from backend.main import create_app
from backend.models import CaseRecord


@pytest.fixture
def cli_response(monkeypatch):
    record = CaseRecord(company={"value": "Example Limited", "document_id": "SOURCE_DOCUMENT", "page": 2}).model_dump(mode="json")
    data = {"record": record, "subject": "Example order", "page_count": 2,
            "evidence": [{"field": "company", "page": 2, "excerpt": "Example Limited"}]}
    data['extraction_error'] = None
    data['field_review'] = {field: {
        'status': 'extracted' if value and value != 'unknown' else 'not_documented',
        'reason': 'Supported by the PDF.' if value and value != 'unknown' else 'Not stated in the PDF.',
        'sources': [{'document_id': 'SOURCE_DOCUMENT', 'page': 2, 'excerpt': 'Example Limited'}]
                   if value and value != 'unknown' else [],
    } for field, value in record.items()}
    state = {"calls": [], "data": data, "returncode": 0, "failure": None}

    def run(command, **kwargs):
        workspace = Path(kwargs['cwd'])
        state['calls'].append({'command': command, 'options': kwargs,
                               'pdf': (workspace/'document.pdf').read_bytes(),
                               'schema': json.loads((workspace/'schema.json').read_text())})
        if state['failure'] == 'timeout':
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        if state['failure'] == 'os_error':
            raise OSError('Cannot launch executable')
        if state['failure'] != 'missing_output':
            output = Path(command[command.index('--output-last-message')+1])
            output.write_text('not JSON' if state['failure'] == 'invalid_json' else json.dumps(state['data']), encoding='utf-8')
        return subprocess.CompletedProcess(command, state['returncode'])

    monkeypatch.setattr(extraction.shutil, 'which', lambda executable: 'C:/tools/codex.exe')
    monkeypatch.setattr(extraction.subprocess, 'run', run)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('CODEX_MODEL', raising=False)
    monkeypatch.delenv('CODEX_TIMEOUT_SECONDS', raising=False)
    return state


def test_pdf_sent_directly_and_fields_loaded_from_database(tmp_path, monkeypatch, cli_response):
    pdf = b"%PDF-test-direct-input"
    with TestClient(create_app(tmp_path)) as client:
        uploaded = client.post("/api/documents", files={"file": ("order.pdf", pdf, "application/pdf")})
        assert uploaded.status_code == 200, uploaded.text
        ids = uploaded.json()
        call = cli_response['calls'][0]
        assert call['pdf'] == pdf
        assert call['schema']['properties']['record']['additionalProperties'] is False
        assert '--output-schema' in call['command']
        assert '--model' not in call['command']
        assert call['command'][-1] == '-'
        assert 'document.pdf' in call['options']['input']
        assert 'OPENAI_API_KEY' not in call['options']['env']
        assert not Path(call['options']['cwd']).exists()
        with connection(tmp_path, database_url="") as db:
            saved = json.loads(db.execute("SELECT record FROM cases WHERE id=?", (ids["case_id"],)).fetchone()["record"])
            assert saved["company"]["document_id"] == ids["document_id"]
            saved["company"]["value"] = "Saved database value"
            db.execute("UPDATE cases SET record=? WHERE id=?", (json.dumps(saved), ids["case_id"]))
        assert client.get(f'/api/cases/{ids["case_id"]}').json()["record"]["company"]["value"] == "Saved database value"
        assert client.get(f'/api/documents/{ids["document_id"]}/content').status_code == 200
        review = client.get(f'/api/cases/{ids["case_id"]}').json()['documents'][0]['extraction']['case_facts']['field_review']
        assert set(review) == set(CaseRecord.model_fields)
        assert review['company']['sources'][0]['document_id'] == ids['document_id']
        assert review['cin']['status'] == 'not_documented'
        assert len(cli_response["calls"]) == 1
        duplicate = client.post("/api/documents", files={"file": ("order.pdf", pdf)})
        assert duplicate.json()["duplicate"] is True
        assert len(cli_response["calls"]) == 1
    # Restart reads stored fields without requiring an Codex CLI connection.
    monkeypatch.setattr(extraction.shutil, "which", lambda executable: None)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f'/api/cases/{ids["case_id"]}').json()["record"]["company"]["value"] == "Saved database value"
        assert len(cli_response["calls"]) == 1


@pytest.mark.parametrize("failure", ["missing_cli", "exit_error", "timeout", "missing_output", "invalid_json", "os_error", "invalid_page", "invalid_record", "unreadable_pdf", "missing_review", "wrong_review_status", "invalid_review_page"])
def test_failure_saves_no_partial_case(tmp_path, monkeypatch, cli_response, failure):
    if failure == 'missing_cli':
        monkeypatch.setattr(extraction.shutil, 'which', lambda executable: None)
    elif failure == 'exit_error':
        cli_response['returncode'] = 1
    elif failure == 'invalid_page':
        cli_response['data']['record']['company']['page'] = 3
    elif failure == 'invalid_record':
        cli_response['data']['record']['unexpected'] = 'bad'
    elif failure == 'unreadable_pdf':
        cli_response['data']['extraction_error'] = 'Unable to read scanned pages'
    elif failure == 'missing_review':
        del cli_response['data']['field_review']['cin']
    elif failure == 'wrong_review_status':
        cli_response['data']['field_review']['company']['status'] = 'not_documented'
    elif failure == 'invalid_review_page':
        cli_response['data']['field_review']['company']['sources'][0]['page'] = 99
    else:
        cli_response['failure'] = failure
    with TestClient(create_app(tmp_path)) as client:
        result = client.post("/api/documents", files={"file": ("order.pdf", b"%PDF-test")})
        assert result.status_code == 422
        assert client.get("/api/cases").json() == []
        with connection(tmp_path, database_url="") as db:
            assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_explicit_extraction_saves_new_response(tmp_path, cli_response):
    with TestClient(create_app(tmp_path)) as client:
        ids = client.post("/api/documents", files={"file": ("order.pdf", b"%PDF-test")}).json()
        cli_response["data"]["record"]["company"]["value"] = "Updated from Codex CLI"
        refreshed = client.post(f'/api/documents/{ids["document_id"]}/extract-facts')
        assert refreshed.status_code == 200, refreshed.text
        detail = client.get(f'/api/cases/{ids["case_id"]}').json()
        assert detail["record"]["company"]["value"] == "Updated from Codex CLI"
        assert detail["version"] == 2
        assert len(cli_response["calls"]) == 2


def test_schema_requires_all_form_fields():
    schema = extraction.response_schema()
    assert set(schema["properties"]["record"]["required"]) == set(CaseRecord.model_fields)
    assert set(schema['properties']['field_review']['required']) == set(CaseRecord.model_fields)
    assert set(extraction.FIELD_DESCRIPTIONS) == set(CaseRecord.model_fields)
    for definition in schema['$defs'].values():
        if definition.get('type') == 'object':
            assert set(definition['required']) == set(definition['properties'])
    serialized = json.dumps(schema)
    for unsupported in ('(?=', '(?!', '(?<=', '(?<!'):
        assert unsupported not in serialized


def test_output_schema_covers_screen_fields():
    import re
    javascript = (Path(__file__).resolve().parents[1] / 'frontend/app.js').read_text(encoding='utf-8')
    screen = javascript.split('function renderDetail()', 1)[1].split('function renderPage()', 1)[0]
    displayed = set(re.findall(r"\['([a-z_]+)','[^']+'\]", screen))
    displayed.update(re.findall(r'\br\.([a-z_]+)', screen))
    displayed.update({'claims', 'plans', 'asset_values', 'plan_payments', 'notes', 'outcome'})
    schema = extraction.response_schema()
    assert displayed <= set(schema['properties']['record']['required'])
    assert displayed <= set(schema['properties']['field_review']['required'])


def test_merge_preserves_entered_values():
    current = CaseRecord().model_dump(mode="json")
    new = copy.deepcopy(current)
    current["company"] = {"value": "Entered", "document_id": "old", "page": 1}
    new["company"] = {"value": "Suggested", "document_id": "new", "page": 1}
    assert extraction.fill_missing(current, new)["company"]["value"] == "Entered"
