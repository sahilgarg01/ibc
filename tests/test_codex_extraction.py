import copy
import json

import asyncio
from openai_codex import CodexError, ApprovalMode, Sandbox, TurnResult
from openai_codex.types import TurnStatus
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from backend import codex_extraction as extraction
from backend.db import connection
from backend.main import create_app
from backend.models import CaseRecord


@pytest.fixture
def sdk_response(monkeypatch):
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
    state = {"calls": [], "data": data, "failure": None, "closed": 0}

    class FakeCodex:
        def __init__(self, config):
            self.config = config

        async def thread_start(self, **options):
            self.options = options
            if state['failure'] == 'sdk_error':
                raise CodexError('Runtime failed')
            return self

        async def run(self, prompt, *, output_schema):
            workspace = Path(self.options['cwd'])
            state['calls'].append({'options': self.options, 'config': self.config,
                                   'prompt': prompt, 'schema': output_schema,
                                   'pdf': (workspace/'document.pdf').read_bytes()})
            if state['failure'] == 'timeout':
                await asyncio.sleep(10)
            if state['failure'] == 'os_error':
                raise OSError('Runtime unavailable')
            if state['failure'] == 'runtime_error':
                raise RuntimeError('SDK turn failed')
            output = 'not JSON' if state['failure'] == 'invalid_json' else json.dumps(state['data'])
            return TurnResult(
                id='test-turn',
                status=TurnStatus.interrupted if state['failure'] == 'incomplete' else TurnStatus.completed,
                error=None, final_response=None if state['failure'] == 'missing_response' else output,
                started_at=None, completed_at=None, duration_ms=None, items=[], usage=None,
            )

        async def close(self):
            state['closed'] += 1

    monkeypatch.setattr(extraction, 'AsyncCodex', FakeCodex)
    monkeypatch.delenv('CODEX_MODEL', raising=False)
    monkeypatch.delenv('CODEX_TIMEOUT_SECONDS', raising=False)
    return state


def test_pdf_sent_directly_and_fields_loaded_from_database(tmp_path, monkeypatch, sdk_response):
    pdf = (Path(__file__).resolve().parents[1] / "samples/indo-global-resolution-order.pdf").read_bytes()
    with TestClient(create_app(tmp_path)) as client:
        uploaded = client.post("/api/documents", files={"file": ("order.pdf", pdf, "application/pdf")})
        assert uploaded.status_code == 200, uploaded.text
        ids = uploaded.json()
        call = sdk_response['calls'][0]
        assert call['pdf'] == pdf
        assert call['schema']['properties']['record']['additionalProperties'] is False
        assert call['options']['model'] is None
        assert call['options']['sandbox'] == Sandbox.workspace_write
        assert call['options']['approval_mode'] == ApprovalMode.deny_all
        assert call['options']['ephemeral'] is True
        assert 'document.pdf' in call['prompt']
        assert call['config'].env['OPENAI_API_KEY'] == ''
        assert call['config'].codex_bin is None
        assert not Path(call['options']['cwd']).exists()
        assert sdk_response['closed'] == 1
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
        assert len(sdk_response["calls"]) == 1
        duplicate = client.post("/api/documents", files={"file": ("order.pdf", pdf)})
        assert duplicate.json()["duplicate"] is True
        assert len(sdk_response["calls"]) == 1
    # Restart reads stored fields without requiring an Codex SDK connection.
    monkeypatch.setattr(extraction, 'AsyncCodex', lambda _: (_ for _ in ()).throw(AssertionError('SDK must not run on reads')))
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f'/api/cases/{ids["case_id"]}').json()["record"]["company"]["value"] == "Saved database value"
        assert len(sdk_response["calls"]) == 1


@pytest.mark.parametrize("failure", ["sdk_error", "runtime_error", "timeout", "missing_response", "incomplete", "invalid_json", "os_error", "invalid_page", "invalid_record", "unreadable_pdf", "missing_review", "wrong_review_status", "invalid_review_page"])
def test_failure_saves_no_partial_case(tmp_path, monkeypatch, sdk_response, failure):
    if failure == 'timeout':
        monkeypatch.setenv('CODEX_TIMEOUT_SECONDS', '1')
        sdk_response['failure'] = failure
    elif failure == 'invalid_page':
        sdk_response['data']['record']['company']['page'] = 3
    elif failure == 'invalid_record':
        sdk_response['data']['record']['unexpected'] = 'bad'
    elif failure == 'unreadable_pdf':
        sdk_response['data']['extraction_error'] = 'Unable to read scanned pages'
    elif failure == 'missing_review':
        del sdk_response['data']['field_review']['cin']
    elif failure == 'wrong_review_status':
        sdk_response['data']['field_review']['company']['status'] = 'not_documented'
    elif failure == 'invalid_review_page':
        sdk_response['data']['field_review']['company']['sources'][0]['page'] = 99
    else:
        sdk_response['failure'] = failure
    with TestClient(create_app(tmp_path)) as client:
        result = client.post("/api/documents", files={"file": ("order.pdf", b"%PDF-test")})
        assert result.status_code == 422
        assert client.get("/api/cases").json() == []
        assert sdk_response['closed'] == 1
        with connection(tmp_path, database_url="") as db:
            assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_explicit_extraction_saves_new_response(tmp_path, sdk_response):
    with TestClient(create_app(tmp_path)) as client:
        ids = client.post("/api/documents", files={"file": ("order.pdf", b"%PDF-test")}).json()
        sdk_response["data"]["record"]["company"]["value"] = "Updated from Codex SDK"
        refreshed = client.post(f'/api/documents/{ids["document_id"]}/extract-facts')
        assert refreshed.status_code == 200, refreshed.text
        detail = client.get(f'/api/cases/{ids["case_id"]}').json()
        assert detail["record"]["company"]["value"] == "Updated from Codex SDK"
        assert detail["version"] == 2
        assert len(sdk_response["calls"]) == 2


def test_recoverable_sdk_claim_conflicts_do_not_reject_pdf(tmp_path, sdk_response):
    source = {"document_id": "SOURCE_DOCUMENT", "page": 2}
    sdk_response["data"]["record"]["claims"] = [
        {"creditor": "Unsecured financial creditors", "category": "unsecured_financial",
         "scope": "category_total", "claimed": "300", "admitted": "250",
         "plan_amount": "100", "actual_paid": None, **source, "notes": "Category total"},
        {"creditor": "Example Bank", "category": "unsecured_financial",
         "scope": "individual", "claimed": "200", "admitted": "150",
         "plan_amount": "75", "actual_paid": None, **source, "notes": "Named creditor"},
    ]
    sdk_response["data"]["record"]["notes"] = "Review the allocation table."
    sdk_response["data"]["field_review"]["claims"] = {
        "status": "extracted", "reason": "Claims table on page 2.",
        "sources": [{**source, "excerpt": "Unsecured financial creditors"}],
    }
    # Generated review notes do not need to masquerade as a sourced PDF fact.
    sdk_response["data"]["field_review"]["notes"] = {
        "status": "not_documented", "reason": "Reviewer guidance.", "sources": [],
    }
    with TestClient(create_app(tmp_path)) as client:
        uploaded = client.post("/api/documents", files={"file": ("order.pdf", b"%PDF-test")})
        assert uploaded.status_code == 200, uploaded.text
        detail = client.get('/api/cases/' + uploaded.json()['case_id']).json()
        assert [claim['creditor'] for claim in detail['record']['claims']] == ['Example Bank']
        warnings = detail['documents'][0]['extraction']['warnings']
        assert any('category-total claim' in warning for warning in warnings)


def test_repeated_sdk_category_totals_keep_most_complete_row(tmp_path, sdk_response):
    source = {"document_id": "SOURCE_DOCUMENT", "page": 2}
    sdk_response["data"]["record"]["claims"] = [
        {"creditor": "Unsecured financial creditors", "category": "unsecured_financial",
         "scope": "category_total", "claimed": None, "admitted": "250",
         "plan_amount": None, "actual_paid": None, **source, "notes": "Partial total"},
        {"creditor": "Unsecured financial creditors total", "category": "unsecured_financial",
         "scope": "category_total", "claimed": "300", "admitted": "250",
         "plan_amount": "100", "actual_paid": None, **source, "notes": "Complete total"},
    ]
    sdk_response["data"]["field_review"]["claims"] = {
        "status": "extracted", "reason": "Claims table on page 2.",
        "sources": [{**source, "excerpt": "Unsecured financial creditors"}],
    }
    with TestClient(create_app(tmp_path)) as client:
        uploaded = client.post("/api/documents", files={"file": ("order.pdf", b"%PDF-test")})
        assert uploaded.status_code == 200, uploaded.text
        detail = client.get('/api/cases/' + uploaded.json()['case_id']).json()
        assert len(detail['record']['claims']) == 1
        assert detail['record']['claims'][0]['creditor'] == 'Unsecured financial creditors total'
        assert any('removed repeated totals' in warning
                   for warning in detail['documents'][0]['extraction']['warnings'])


def test_invalid_sdk_summary_evidence_does_not_reject_valid_fields(tmp_path, sdk_response):
    sdk_response['data']['evidence'] = [
        {'field': 'company.value', 'page': 2, 'excerpt': ' Example Limited '},
        {'field': 'claims[0].admitted', 'page': 2, 'excerpt': 'Admitted amount'},
        {'field': 'not_a_form_field', 'page': 2, 'excerpt': 'Ignore this'},
        {'field': 'company', 'page': 99, 'excerpt': 'Wrong page'},
        {'field': 'company', 'page': 2, 'excerpt': '   '},
    ]
    with TestClient(create_app(tmp_path)) as client:
        uploaded = client.post('/api/documents', files={'file': ('order.pdf', b'%PDF-test')})
        assert uploaded.status_code == 200, uploaded.text
        detail = client.get('/api/cases/' + uploaded.json()['case_id']).json()
        extraction = detail['documents'][0]['extraction']
        assert extraction['case_facts']['evidence'] == [
            {'field': 'company', 'page': 2, 'excerpt': 'Example Limited'},
            {'field': 'claims', 'page': 2, 'excerpt': 'Admitted amount'},
        ]
        assert any('Normalized 2 nested' in warning for warning in extraction['warnings'])
        assert any('Discarded 3 unusable' in warning for warning in extraction['warnings'])


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


def test_sdk_uses_configured_model(monkeypatch, sdk_response):
    monkeypatch.setenv('CODEX_MODEL', 'gpt-6.1-sol')
    result = extraction.extract_pdf(b'%PDF-test')
    assert sdk_response['calls'][0]['options']['model'] == 'gpt-6.1-sol'
    assert result['provider'] == 'codex_sdk'
    assert result['model'] == 'gpt-6.1-sol'


def test_usage_and_processing_metadata_are_saved(monkeypatch, sdk_response):
    usage = {'input_tokens': 120, 'cached_input_tokens': 20, 'output_tokens': 30,
             'reasoning_output_tokens': 10, 'total_tokens': 150}

    async def request(*args):
        return json.dumps(sdk_response['data']), usage

    monkeypatch.setattr(extraction, 'request_fields', request)
    result = extraction.extract_pdf(b'%PDF-test')
    assert result['usage'] == usage
    assert result['file_bytes'] == 9
    assert result['processing_seconds'] >= 0
