"""New PDFs travel through the real reader and ingestion path, without backfills."""
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from backend.main import create_app
from backend.models import CaseRecord
from backend.db import connection


def make_pdf(pages):
    writer = PdfWriter()
    for lines in pages:
        page = writer.add_blank_page(width=1000, height=792)
        font = DictionaryObject({NameObject('/Type'):NameObject('/Font'), NameObject('/Subtype'):NameObject('/Type1'), NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        content = ['BT /F1 9 Tf 30 760 Td 13 TL']
        for line in lines:
            escaped = line.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
            content.append('('+escaped+') Tj T*')
        content.append('ET')
        stream = DecodedStreamObject()
        stream.set_data('\n'.join(content).encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def future_document(company, wrapped=False, omit=()):
    scalar = [
        ('Corporate debtor','Company name',company),
        ('Debtor CIN','Debtor CIN','U12345MH2010PTC123456'),
        ('Corporate debtor registered office','Registered office of corporate debtor','10 Market Road, Mumbai 400001'),
        ('Industry','Sector','Industrial machinery'),
        ('Case number','Case no.','CP 501/2025'),
        ('Tribunal','NCLT bench','NCLT Mumbai Court II'),
        ('Admission date','CIRP commencement date','01.02.2025'),
        ('Resolution approval date','Date of resolution plan approval','02.03.2026'),
        ('Order date','Date of order','02.03.2026'),
        ('Interim resolution professional','IRP','Anita Rao'),
        ('Resolution professional','RP','Meera Shah'),
        ('Liquidator','Name of liquidator','Dev Patel'),
        ('Successful resolution applicant','Name of successful resolution applicant (SRA)','Summit Holdings Limited'),
        ('Resolution applicant CIN','Resolution applicant CIN','U54321MH2012PLC654321'),
        ('Fair value','Total fair value','Rs 10 crore/-'),
        ('Liquidation value','Average liquidation value','Rs 8 crore/-'),
        ('Total claims received','Total claims filed','Rs 20 crore'),
        ('Total admitted claims','Total claims admitted','Rs 15 crore'),
        ('CoC voting share','CoC voting share','91.75%'),
        ('Applicant incorporation date','Date of incorporation of SRA','05.06.2012'),
        ('Applicant business','Nature of business of SRA','Engineering services'),
        ('Applicant registered office','Location of SRA','20 Lake Road, Pune 400002'),
    ]
    pages = [['IN THE NATIONAL COMPANY LAW TRIBUNAL'],[]]
    for index, (label, alternate, value) in enumerate(scalar):
        if label in omit:
            continue
        page = pages[0 if index < 12 else 1]
        if wrapped:
            value = {'Admission date':'1st February, 2025',
                'Resolution approval date':'2nd March, 2026', 'Order date':'March 2, 2026',
                'Applicant incorporation date':'June 5, 2012',
                'Fair value':'INR 1 0,00,00,000/-'}.get(label,value)
            page.extend([alternate+':',value])
        else:
            page.append(label+': '+value)
    pages.append([
        'Applicant director: Arun Shah; DIN: 23456789',
        'Judge: Justice Kavita Rao',
        'Withdrawn plan submitter: Cancelled Holdings Limited',
        'Financial creditor: First Bank Limited',
        'Plan submitter: Summit Holdings Limited',
        'Previous owner: Past Holding Limited',
        'Current owner: Current Holding Limited',
        'Subsequent owner: Later Holding Limited',
        'Previous director: Prior Director',
        'Current director: Current Director',
        'Subsequent director: Later Director',
        'Proposed owner: Future Holding Limited',
        'Proposed director: Future Director',
        'Creditor: First Bank Limited; Type: secured financial; Claimed: Rs 20 crore; Admitted: Rs 15 crore; Plan amount: Rs 6 crore; Actual paid: Rs 1 crore',
        'Plan amount: Rs 6 crore',
    ])
    pages.append([
        'Summary of the payments to be made under the Resolution Plan',
        'Sr. No.',
        '1. First Bank Limited 150000000 60000000',
        'Total Plan Value 60000000',
        'The Resolution Plan is hereby approved.',
    ])
    return make_pdf(pages)


def assert_complete_new_draft(detail, company):
    r = detail['record']
    missing = [field for field in CaseRecord.model_fields if field != 'notes' and (not r[field] or r[field] == 'unknown')]
    assert missing == [], 'Fields explicitly present in the new PDF were lost: '+str(missing)
    assert r['company']['value'] == company
    assert r['cin']['value'] == 'U12345MH2010PTC123456'
    assert r['applicant_cin']['value'] == 'U54321MH2012PLC654321'
    for field, (value, page) in {
        'company':(company,1), 'cin':('U12345MH2010PTC123456',1),
        'registered_office':('10 Market Road, Mumbai 400001',1),
        'industry':('Industrial machinery',1), 'case_number':('CP 501/2025',1),
        'tribunal':('NCLT Mumbai Court II',1), 'irp':('Anita Rao',1),
        'professional':('Meera Shah',1), 'liquidator':('Dev Patel',1),
        'applicant':('Summit Holdings Limited',2), 'applicant_cin':('U54321MH2012PLC654321',2),
        'applicant_location':('20 Lake Road, Pune 400002',2),
        'applicant_business':('Engineering services',2), 'coc_voting_share':('91.75%',2),
    }.items():
        assert (r[field]['value'],r[field]['page']) == (value,page),field
    for field, value in {
        'applicant_directors':'Arun Shah', 'judges':'Justice Kavita Rao',
        'withdrawn_submitters':'Cancelled Holdings Limited', 'financial_creditors':'First Bank Limited',
        'plan_submitters':'Summit Holdings Limited', 'previous_owners':'Past Holding Limited',
        'current_owners':'Current Holding Limited', 'subsequent_owners':'Later Holding Limited',
        'previous_directors':'Prior Director', 'current_directors':'Current Director',
        'subsequent_directors':'Later Director', 'proposed_owners':'Future Holding Limited',
        'proposed_directors':'Future Director',
    }.items():
        assert [v['value'] for v in r[field]] == [value],field
        assert r[field][0]['page'] == 3,field
    assert r['applicant_business']['value'] == 'Engineering services'
    assert r['admission_date']['value'] == '2025-02-01'
    assert r['resolution_date']['value'] == '2026-03-02'
    assert r['applicant_incorporated']['value'] == '2012-06-05'
    assert r['fair_value']['value'] == '100000000.00'
    assert r['liquidation_value']['value'] == '80000000.00'
    assert r['total_received_claims']['value'] == '200000000.00'
    assert r['total_admitted_claims']['value'] == '150000000.00'
    assert r['applicant_directors'][0]['din'] == '23456789'
    assert r['claims'][0]['claimed'] == '200000000.00'
    assert r['claims'][0]['admitted'] == '150000000.00'
    assert r['claims'][0]['actual_paid'] == '10000000.00'
    assert r['plans'][0]['amount'] == '60000000.00'
    assert r['plans'][0]['admitted'] == '150000000.00'
    assert r['plan_payments'][0]['amount'] == '60000000.00'
    assert r['plan_payments'][0]['page'] == 4
    assert r['asset_values'][0]['fair_value'] == '100000000.00'
    assert r['asset_values'][0]['liquidation_value'] == '80000000.00'
    assert r['asset_values'][0]['page'] == 2
    assert r['outcome'] == 'resolution_approved'
    assert r['outcome_evidence']['page'] == 4
    doc = detail['documents'][0]
    def evidence(value):
        if isinstance(value,dict):
            if 'document_id' in value:
                assert value['document_id'] == doc['id']
                assert 1 <= value['page'] <= 4
            for child in value.values(): evidence(child)
        elif isinstance(value,list):
            for child in value: evidence(child)
    evidence(r)
    assert detail['version'] == 1  # Filled during ingestion; no repair/reopen pass.
    assert [a['action'] for a in detail['audit']] == ['document_added']
    report = doc['extraction']['case_facts']
    assert set(report['field_review']) == set(CaseRecord.model_fields) - {'notes'}
    assert all(item['status'] == 'extracted' for item in report['field_review'].values())


def test_new_liquidation_upload_handles_screenshot_caption_and_reports_all_fields(tmp_path):
    import json
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent/'fixtures/marks-pryor-liquidation.json').read_text())
    # Rebuild an unseen PDF from the real order's text; the test exercises the
    # actual PDF reader, upload transaction, draft and persisted coverage report.
    pages = [[line.replace('…', '...').encode('ascii', errors='replace').decode()
              for line in page['text'].splitlines()] for page in fixture['pages']]
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/documents', files={'file': ('future-liquidation.pdf', make_pdf(pages), 'application/pdf')})
        assert response.status_code == 200, response.text
        ids = response.json()
        detail = client.get('/api/cases/'+ids['case_id']).json()
        r = detail['record']
        assert r['case_number']['value'] == 'C.P.(IB)/600/(MB)/2023'
        assert r['professional']['value'] == 'Mandar S. Wagh'
        assert r['irp']['value'] == 'Mandar S. Wagh'
        assert r['liquidator']['value'] == 'Atul Narayan Naik'
        assert r['liquidator']['page'] == 7
        assert len(r['claims']) == 2
        report = detail['documents'][0]['extraction']['case_facts']
        assert set(report['field_review']) == set(CaseRecord.model_fields) - {'notes'}
        assert report['field_review']['resolution_date']['status'] == 'not_applicable'
        assert report['field_review']['admission_date']['status'] == 'not_found'
        assert detail['version'] == 1
        assert [a['action'] for a in detail['audit']] == ['document_added']


@pytest.mark.parametrize('company,wrapped', [('Northstar Manufacturing Private Limited',False), ('Riverton Textile Limited',True)])
def test_new_upload_fills_every_present_form_field_on_first_ingestion(tmp_path, company, wrapped):
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/documents', files={'file':('new-unseen.pdf',future_document(company,wrapped),'application/pdf')})
        assert response.status_code == 200, response.text
        detail = client.get('/api/cases/'+response.json()['case_id'])
        assert detail.status_code == 200, detail.text
        assert_complete_new_draft(detail.json(),company)


def test_reading_saved_draft_does_not_apply_new_extraction_rules(tmp_path, monkeypatch):
    import json
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/documents',files={'file':('saved.pdf',future_document('Existing Company Limited'),'application/pdf')})
        response.raise_for_status()
        ids = response.json()
        with connection(tmp_path,database_url='') as db:
            before = dict(db.execute('SELECT record,version FROM cases WHERE id=?',(ids['case_id'],)).fetchone())
            extraction = json.loads(db.execute('SELECT extraction FROM documents WHERE id=?',(ids['document_id'],)).fetchone()[0])
            extraction['case_facts']['version']='old-rules'
            extraction['pages'][0]['text']=''
            extraction['pages'][0].pop('ocr_attempted',None)
            db.execute('UPDATE documents SET extraction=? WHERE id=?',(json.dumps(extraction),ids['document_id']))
        monkeypatch.setattr('backend.main.extract_case',lambda *args: (_ for _ in ()).throw(AssertionError('Read attempted to rewrite existing draft')))
        assert client.get('/api/cases/'+ids['case_id']).status_code==200
        assert client.get('/api/documents/'+ids['document_id']+'/content').status_code==200
        with connection(tmp_path,database_url='') as db:
            after = dict(db.execute('SELECT record,version FROM cases WHERE id=?',(ids['case_id'],)).fetchone())
            assert before == after
            assert db.execute('SELECT count(*) FROM audit WHERE case_id=?',(ids['case_id'],)).fetchone()[0]==1


def test_consecutive_new_pdfs_fill_independently_and_never_rewrite_previous_drafts(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        first_pdf = future_document('First Future Company Limited')
        first = client.post('/api/documents',files={'file':('first.pdf',first_pdf,'application/pdf')})
        first.raise_for_status()
        first_id = first.json()['case_id']
        first_detail = client.get('/api/cases/'+first_id).json()
        assert_complete_new_draft(first_detail,'First Future Company Limited')
        second = client.post('/api/documents',files={'file':('second.pdf',future_document('Second Future Company Limited',True),'application/pdf')})
        second.raise_for_status()
        assert second.json()['case_id'] != first_id
        assert second.json()['document_id'] != first.json()['document_id']
        assert_complete_new_draft(client.get('/api/cases/'+second.json()['case_id']).json(),'Second Future Company Limited')
        third = client.post('/api/documents',files={'file':('third.pdf',future_document('Third Future Company Limited',omit={'Industry','Liquidator'}),'application/pdf')})
        third.raise_for_status()
        third_record = client.get('/api/cases/'+third.json()['case_id']).json()['record']
        assert third_record['company']['value'] == 'Third Future Company Limited'
        assert third_record['industry'] is None  # Unstated fields cannot leak from the previous PDF.
        assert third_record['liquidator'] is None
        duplicate = client.post('/api/documents',files={'file':('first-again.pdf',first_pdf,'application/pdf')})
        duplicate.raise_for_status()
        assert duplicate.json()['duplicate'] is True
        unchanged = client.get('/api/cases/'+first_id).json()
        assert unchanged['record'] == first_detail['record']
        assert unchanged['version'] == first_detail['version']
        assert unchanged['audit'] == first_detail['audit']


def wait_for_import(client, job_id):
    import time
    for _ in range(500):
        response = client.get('/api/imports/'+job_id)
        response.raise_for_status()
        job = response.json()
        if job['status'] not in {'queued','running'}:
            assert job['status'] == 'completed', job
            return job
        time.sleep(.02)
    raise AssertionError('Isolated import did not finish')


@pytest.mark.parametrize('official', [False, True])
def test_future_url_import_populates_the_same_complete_draft(tmp_path, monkeypatch, official):
    company = 'Upcoming URL Industries Limited'
    pdf = future_document(company, wrapped=True)
    url = ('https://ibbi.gov.in/uploads/order/new.pdf' if official else 'https://example.org/new.pdf')
    monkeypatch.setattr('backend.main.public_url',lambda value:value)
    monkeypatch.setattr('backend.url_import.fetch',lambda client,source,limit:(pdf,source))
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/imports',json={'url':url,'max_pages':1,'max_files':1})
        assert response.status_code == 202, response.text
        job = wait_for_import(client,response.json()['id'])
        assert job['result']['imported'] == 1
        cases = client.get('/api/cases').json()
        assert len(cases) == 1
        detail = client.get('/api/cases/'+cases[0]['id']).json()
        assert_complete_new_draft(detail,company)


def test_future_automatic_collection_populates_the_same_complete_draft(tmp_path, monkeypatch):
    company = 'Upcoming Collection Industries Limited'
    pdf = future_document(company)
    listing = b'''<table class="reporttable"><tbody><tr><td>1</td><td>20 Oct, 2026</td><td><a href="/uploads/order/new-complete.pdf">Approval of resolution plan</a></td><td>Final order</td></tr></tbody></table>'''
    monkeypatch.setattr('backend.automatic_orders.fetch',lambda client,url,limit:((pdf if url.endswith('.pdf') else listing),url))
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/scheduler/sync?category=nclt')
        assert response.status_code == 202, response.text
        job = wait_for_import(client,response.json()['id'])
        assert job['result']['imported'] == 1
        cases = client.get('/api/cases').json()
        assert len(cases) == 1
        detail = client.get('/api/cases/'+cases[0]['id']).json()
        assert_complete_new_draft(detail,company)
