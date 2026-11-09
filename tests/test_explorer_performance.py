"""Keep dashboard reads independent of large draft evidence records."""
import json

from fastapi.testclient import TestClient

from backend.db import Database
from backend.main import create_app
from backend.models import CaseRecord


def test_case_summary_preserves_list_fields_and_downloads(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        db = client.app.state.application.database
        record = CaseRecord().model_dump(mode='json')
        record['notes'] = 'Long evidence notes. ' * 200
        with db.session() as session:
            session.insert_case('draft', json.dumps(record), Database.timestamp())
        full = client.get('/api/cases').json()
        summary = client.get('/api/cases?summary=true').json()
        assert summary[0]['metrics'] == full[0]['metrics']
        assert summary[0]['pdf_documents'] == full[0]['pdf_documents']
        assert summary[0]['subjects'] == full[0]['subjects']
        for key in summary[0]['record']:
            assert summary[0]['record'][key] == full[0]['record'][key]
        assert len(json.dumps(summary)) < len(json.dumps(full)) / 4
        assert client.get('/api/cases/draft').json()['record']['notes'] == record['notes']


def test_analytics_reads_approved_records_without_document_scans(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path)) as client:
        db = client.app.state.application.database
        with db.session() as session:
            session.insert_case('draft', CaseRecord().model_dump_json(), Database.timestamp())
            session.insert_case('approved', CaseRecord().model_dump_json(), Database.timestamp())
            session.execute("UPDATE cases SET review_status='approved' WHERE id='approved'")
        original = Database.filtered_cases
        statuses = []

        def filtered(self, status='all'):
            statuses.append(status)
            return original(self, status)

        def no_documents(self):
            raise AssertionError('Analytics must not scan documents')

        monkeypatch.setattr(Database, 'filtered_cases', filtered)
        monkeypatch.setattr(Database, 'case_subjects', no_documents)
        monkeypatch.setattr(Database, 'case_document_summaries', no_documents)
        result = client.get('/api/analytics').json()
        assert statuses == ['approved']
        assert result['total_cases'] == 2
        assert result['approved_cases'] == 1
        assert result['draft_cases'] == 1
