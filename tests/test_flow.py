from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import ROOT, create_app


def test_sample_review_flow(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        imported = client.post('/api/sample/import')
        assert imported.status_code == 200, imported.text
        case_id = imported.json()['case_id']
        detail = client.get(f'/api/cases/{case_id}').json()
        assert detail['review_status'] == 'draft'
        assert detail['record']['company']['value'].startswith('Indo Global')
        assert detail['record']['cin'] is None  # Page 8 CIN belongs to the buyer.
        assert detail['record']['irp']['value'].startswith('Shailen Shah')
        assert len(detail['documents'][0]['extraction']['pages']) == 23
        assert 'Secured' in detail['documents'][0]['extraction']['pages'][8]['text']
        assert detail['metrics']['matched_rows'] == 4
        assert detail['metrics']['matched_admitted_inr'] == '8951190638'
        assert detail['metrics']['matched_plan_inr'] == '1452600000'
        assert detail['metrics']['plan_haircut_percent'] == '83.77'
        assert client.get('/api/analytics').json()['approved_cases'] == 0
        assert client.get('/').status_code == 200
        assert client.get('/app.js').status_code == 200

        duplicate = client.post('/api/sample/import')
        assert duplicate.json()['duplicate'] is True
        assert duplicate.json()['case_id'] == case_id

        doc_id = detail['documents'][0]['id']
        invalid = detail['record'].copy()
        invalid['company'] = {'value':'Wrong page','document_id':doc_id,'page':999}
        assert client.put(f'/api/cases/{case_id}',json={'version':1,'record':invalid}).status_code == 422
        assert client.post(f'/api/cases/{case_id}/approve',json={'version':1,'reviewer':'Researcher','confirmed':False}).status_code == 422

        approved = client.post(f'/api/cases/{case_id}/approve',json={'version':1,'reviewer':'Researcher','confirmed':True})
        assert approved.status_code == 200, approved.text
        assert approved.json()['review_status'] == 'approved'
        assert client.get('/api/analytics').json()['weighted_plan_haircut_percent'] == '83.77'
        assert client.post(f'/api/cases/{case_id}/approve',json={'version':1,'reviewer':'Researcher','confirmed':True}).status_code == 409

        record = approved.json()['record']
        record['claims'][0]['actual_paid'] = '100'
        saved = client.put(f'/api/cases/{case_id}',json={'version':2,'record':record})
        assert saved.status_code == 200, saved.text
        assert saved.json()['review_status'] == 'draft'
        assert client.get('/api/analytics').json()['approved_cases'] == 0
        assert len(saved.json()['audit']) >= 3


def test_upload_rejects_non_pdf_and_reuses_document(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        bad = client.post('/api/documents', files={'file':('bad.pdf',b'not a pdf','application/pdf')})
        assert bad.status_code == 422
        pdf = (ROOT / 'samples/indo-global-resolution-order.pdf').read_bytes()
        first = client.post('/api/documents',files={'file':('sample.pdf',pdf,'application/pdf')})
        assert first.status_code == 200, first.text
        duplicate = client.post('/api/documents',files={'file':('same.pdf',pdf,'application/pdf')}).json()
        assert duplicate['duplicate'] is True
        assert duplicate['document_id'] == first.json()['document_id']
        assert duplicate['original']['filename'] == 'sample.pdf'
        assert duplicate['original']['sha256']
        case_id=first.json()['case_id']
        doc_id=first.json()['document_id']
        case=client.get(f'/api/cases/{case_id}').json()
        assert case['record']['company']['value'] == 'Indo Global Soft Solutions and Technologies Private Limited'
        assert len(case['record']['claims']) == 4
        assert case['record']['cin'] is None  # Applicant CIN must not become debtor CIN.
        assert case['record']['plans'][0]['amount'] == '1452600000.00'
        assert client.get(f'/api/documents/{doc_id}/pdf').content.startswith(b'%PDF-')
