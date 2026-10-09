from backend.case_extraction import extract_case, money, fill_missing
from backend.extraction import extract_pdf
from backend.main import ROOT, create_app
from backend.models import metrics, CaseRecord
from backend.db import connection
from fastapi.testclient import TestClient
import json


def test_labeled_facts_claims_ownership_and_plan():
    text='''Corporate debtor: Example Private Limited
Debtor CIN: U12345MH2020PTC123456
Case number: CP 123/2025
Tribunal: NCLT Mumbai
Admission date: 2024-01-01
Resolution approval date: 2025-01-01
Resolution professional: Ravi Shah
Successful resolution applicant: Buyer Ltd
Previous director: Ravi Shah; DIN: 12345678
New director: S. Shah; DIN: 12345678
Previous owner: Old Holding Ltd; CIN: U12345MH2019PTC123456
New owner: Buyer Ltd
Current owner: Present Holding Ltd; CIN: U12345MH2021PTC123456
Current director: Anita Mehta; DIN: 87654321
Creditor: Bank A; Type: secured financial; Claimed: Rs 2 crore; Admitted: INR 1.5 crore; Plan amount: Rs 30 lakh
Plan amount: Rs 30 lakh
The Resolution Plan is hereby approved.
'''
    r,report=extract_case({'pages':[{'page':1,'text':text}]},'doc')
    assert r['company']['value']=='Example Private Limited'
    assert r['previous_directors'][0]['din']=='12345678'
    assert r['current_owners'][0]['value']=='Present Holding Ltd'
    assert r['current_owners'][0]['cin']=='U12345MH2021PTC123456'
    assert r['current_directors'][0]['din']=='87654321'
    assert r['claims'][0]['admitted']=='15000000.00'
    assert r['plans'][0]['amount']=='3000000.00'
    assert r['outcome']=='resolution_approved' and report['evidence']
    assert money('unknown') is None
    assert money('0')=='0.00'


def test_drt_pdf_headings_fill_only_supported_fields():
    text='''BEFORE THE DEBTS RECOVERY TRIBUNAL –II AT CHENNAI
Dated this 8th day of August, 2022
IBC No.1 of 2022
Application against a personal guarantor to Corporate Debtor “Alectrona Energy Private Ltd”.
The plan of another case was approved in 2020.
'''
    record, report=extract_case({'pages':[{'page':1,'text':text}]},'pdf-1')
    assert record['company']['value']=='Alectrona Energy Private Ltd'
    assert record['case_number']['value']=='IBC No. 1 of 2022'
    assert record['tribunal']['value']=='DRT II Chennai'
    assert record['order_date']['value']=='2022-08-08'
    assert all(record[field]['document_id']=='pdf-1' and record[field]['page']==1
               for field in ('company','case_number','tribunal','order_date'))
    assert len(report['evidence'])>=4
    assert record['outcome']=='unknown' and record['claims']==[] and record['plans']==[]


def test_no_inferred_company_from_personal_guarantor_caption():
    text='''INTHEDEBTSRECOVERYTRIBUNALNo.2,MUMBAI
INSOLVENCYAPPLICATION NO.30f2021
Mr Ateev Vrajlal Gala ... Defendant
21st April, 2022.
'''
    record,_=extract_case({'pages':[{'page':1,'text':text}]},'pdf-2')
    assert record['company'] is None
    assert record['case_number']['value']=='Insolvency Application No. 3 of 2021'
    assert record['tribunal']['value']=='DRT 2 Mumbai'
    assert record['order_date']['value']=='2022-04-21'


def test_corporate_debtor_caption_does_not_include_the_other_party():
    text='''In the matter of:
CTC Projects Private Limited.  ....Petitioner-Operational Creditor.
Versus
Hind Inns & Hotel Limited.  ....Respondent-Corporate Debtor.
CP (IB) No.168/Chd/Chd/2018
THE NATIONAL COMPANY LAW TRIBUNAL
CHANDIGARH BENCH, CHANDIGARH
'''
    record,_=extract_case({'pages':[{'page':1,'text':text}]},'pdf-3')
    assert record['company']['value']=='Hind Inns & Hotel Limited'
    assert record['case_number']['value']=='CP (IB) No.168/Chd/Chd/2018'
    assert record['tribunal']['value']=='NCLT Chandigarh'


def test_real_sample_extraction_and_no_invented_ownership():
    extraction=extract_pdf((ROOT/'samples/indo-global-resolution-order.pdf').read_bytes())
    r,report=extract_case(extraction,'doc')
    assert r['company']['value']=='Indo Global Soft Solutions and Technologies Private Limited'
    assert r['cin'] is None
    assert r['previous_owners']==[] and r['subsequent_owners']==[]
    assert len(r['claims'])==4
    assert metrics(CaseRecord.model_validate(r))['plan_haircut_percent']=='83.77'
    assert r['plans'][0]['amount']=='1452600000.00'
    assert r['plans'][0]['page']==9 and r['admission_date']['page']==2
    assert report['version']


def test_owner_roles_need_explicit_corporate_debtor_evidence():
    extraction={'pages':[
        {'page':1,'text':'Current shareholder: Blue Holdings Ltd\nFormer director: Meera Rao; DIN: 12345678'},
        {'page':2,'text':'Raj Shah was the former owner of the corporate debtor\nNew director: Kavya Shah; DIN: 87654321\nRegional Director (Western Region), Ministry of Corporate Affairs'},
    ]}
    record,report=extract_case(extraction,'owner-pdf')
    assert record['current_owners'][0]['value']=='Blue Holdings Ltd'
    assert record['current_owners'][0]['page']==1
    assert record['previous_owners'][0]['value']=='Raj Shah'
    assert record['previous_owners'][0]['page']==2
    assert record['previous_directors'][0]['value']=='Meera Rao'
    assert record['subsequent_directors'][0]['value']=='Kavya Shah'
    assert not record['current_directors']
    assert all(item['field']!='current_directors' for item in report['evidence'])


def test_ownership_from_another_pdf_adds_missing_people_without_replacing_reviewed_names():
    first,_=extract_case({'pages':[{'page':1,'text':'Former director: Meera Rao; DIN: 12345678'}]},'first')
    second,_=extract_case({'pages':[{'page':2,'text':'Former director: Meera Rao; DIN: 12345678\nCurrent owner: Blue Holdings Ltd'}]},'second')
    merged=fill_missing(first,second)
    assert len(merged['previous_directors'])==1
    assert merged['previous_directors'][0]['document_id']=='first'
    assert merged['current_owners'][0]['document_id']=='second'
    assert merged['current_owners'][0]['page']==2


def test_backfill_preserves_entered_values_and_is_idempotent(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        result=client.post('/api/sample/import').json()
        doc_id=result['document_id'];case_id=result['case_id']
        with connection(tmp_path,database_url='') as db:
            r=json.loads(db.execute('SELECT record FROM cases WHERE id=?',(case_id,)).fetchone()[0])
            r['company']['value']='Manually reviewed company'
            r['claims']=[]
            db.execute('UPDATE cases SET record=? WHERE id=?',(json.dumps(r),case_id))
        response=client.post('/api/documents/'+doc_id+'/extract-facts')
        assert response.status_code==200,response.text
        case=client.get('/api/cases/'+case_id).json()
        assert case['record']['company']['value']=='Manually reviewed company'
        assert len(case['record']['claims'])==4
        version=case['version']
        client.post('/api/documents/'+doc_id+'/extract-facts')
        assert client.get('/api/cases/'+case_id).json()['version']==version


def test_current_owner_is_saved_with_its_source_page(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        imported=client.post('/api/sample/import').json()
        case_id,doc_id=imported['case_id'],imported['document_id']
        detail=client.get('/api/cases/'+case_id).json()
        record=detail['record']
        record['current_owners']=[{'value':'Evidence Holdings Ltd','document_id':doc_id,'page':1}]
        saved=client.put('/api/cases/'+case_id,json={'version':detail['version'],'record':record})
        assert saved.status_code==200,saved.text
        assert saved.json()['record']['current_owners'][0]['value']=='Evidence Holdings Ltd'
        with connection(tmp_path,database_url='') as db:
            row=db.execute("SELECT name,period,page FROM people WHERE case_id=? AND role='owner' AND period='current'",(case_id,)).fetchone()
            assert row['name']=='Evidence Holdings Ltd' and row['period']=='current' and row['page']==1
