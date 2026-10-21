from backend.case_extraction import extract_case, money, fill_missing
from backend.extraction import extract_pdf
from backend.main import ROOT, create_app
from backend.models import metrics, CaseRecord
from backend.final_orders import final_decision
from backend.db import connection
from fastapi.testclient import TestClient
import json


def test_labeled_facts_claims_ownership_and_plan():
    text='''IN THE NATIONAL COMPANY LAW TRIBUNAL, MUMBAI
Corporate debtor: Example Private Limited
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


def test_final_order_business_fields_and_total_based_haircut():
    text='''IN THE NATIONAL COMPANY LAW TRIBUNAL, MUMBAI
Corporate debtor: Example Private Limited
Corporate debtor registered office: 1 Market Road, Mumbai
Debtor CIN: U12345MH2020PTC123456
Industry: Textiles
Successful resolution applicant: Buyer Limited
Resolution applicant CIN: U12345MH2021PLC123456
Fair value: Rs 10 crore
Liquidation value: Rs 8 crore
Total admitted claims: Rs 20 crore
Total amount under resolution plan: Rs 5 crore
The Resolution Plan is hereby approved.
'''
    record,_=extract_case({'pages':[{'page':1,'text':text}]},'source-pdf')
    assert record['registered_office']['value']=='1 Market Road, Mumbai'
    assert record['industry']['value']=='Textiles'
    assert record['applicant']['value']=='Buyer Limited'
    assert record['applicant_cin']['value']=='U12345MH2021PLC123456'
    assert record['fair_value']['value']=='100000000.00'
    assert record['liquidation_value']['value']=='80000000.00'
    assert record['plans'][0]['admitted']=='200000000.00'
    result=metrics(CaseRecord.model_validate(record))
    assert result['plan_haircut_percent']=='75.00' and result['basis']=='plan_total'


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
    assert r['applicant_cin']['value']=='U45202PN2020PTC189375'
    assert r['applicant_cin']['page']==8
    assert r['plans'][0]['page']==9 and r['admission_date']['page']==2
    assert report['version']


def test_quoted_nclt_approval_in_high_court_judgment_is_not_a_final_order():
    extraction={'pages':[
        {'page':1,'text':'IN THE HIGH COURT OF GUJARAT AT AHMEDABAD\nJUDGMENT DATED: 09/04/2025\nTECHNOVAA PLASTIC INDUSTRIES PVT. LTD.\nVersus\nTAX OFFICER'},
        {'page':2,'text':'The Petitioner is a company engaged in the business of manufacturing plastic film.\n'
                            'CP (IB) 189 of 2018 was filed against the Petitioner-Corporate Debtor before the '
                            'National Company Law Tribunal ("NCLT"), Ahmedabad. The NCLT, by an order dated '
                            '12.11.2018, admitted the said application.'},
        {'page':3,'text':'The NCLT, by an order dated 04.09.2020, approved the resolution plan '
                            'submitted by Kankriya Enterprises Private Limited. The Resolution Plan is approved. '
                            'The tax demand notices are quashed.'},
    ]}
    assert final_decision(extraction) is None
    record,_=extract_case(extraction,'high-court-pdf')
    assert record['outcome']=='unknown' and record['outcome_evidence'] is None
    assert record['company']['value']=='Technovaa Plastic Industries Pvt. Ltd.'
    assert record['case_number']['value']=='CP (IB) No. 189 of 2018'
    assert record['industry']['value']=='manufacturing plastic film'
    assert record['admission_date']['value']=='2018-11-12'
    assert record['applicant']['value']=='Kankriya Enterprises Private Limited'


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


def test_opening_older_draft_refills_available_fields(tmp_path):
    pdf=(ROOT/'samples/indo-global-resolution-order.pdf').read_bytes()
    with TestClient(create_app(tmp_path)) as client:
        imported=client.post('/api/documents',files={'file':('order.pdf',pdf,'application/pdf')}).json()
        case_id,doc_id=imported['case_id'],imported['document_id']
        with connection(tmp_path,database_url='') as db:
            row=db.execute('SELECT record FROM cases WHERE id=?',(case_id,)).fetchone()
            record=json.loads(row['record'])
            record['applicant_cin']=None
            db.execute('UPDATE cases SET record=? WHERE id=?',(json.dumps(record),case_id))
            row=db.execute('SELECT extraction FROM documents WHERE id=?',(doc_id,)).fetchone()
            extraction=json.loads(row['extraction'])
            extraction['case_facts']['version']='rules-4'
            db.execute('UPDATE documents SET extraction=? WHERE id=?',(json.dumps(extraction),doc_id))
        detail=client.get('/api/cases/'+case_id).json()
        assert detail['record']['applicant_cin']['value']=='U45202PN2020PTC189375'
        assert detail['version']==2
        assert client.get('/api/cases/'+case_id).json()['version']==2


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



def test_narrative_order_reads_later_pages_without_inventing_party_claims():
    pages = [
        {"page": 1, "text": "IN THE NATIONAL COMPANY LAW TRIBUNAL MUMBAI BENCH-II\n"
                             "Guruanand Silk Mills Private Limited ... Corporate Debtor\n"
                             "CP (IB) No. 1069/MB/2023"},
        {"page": 2, "text": "Mr. Krishna Chamadia, (hereinafter referred to as the Applicant/Resolution Professional) "
                             "filed this application. The Resolution Plan, submitted by Siddhachal Fab Private Limited, "
                             "the Successful Resolution Applicant, was approved."},
        {"page": 3, "text": "This Tribunal vide its order dated 14.11.2024 admitted the Corporate Debtor. "
                             "The CoC was constituted with Axis Bank Limited as the Sole Financial Creditor."},
        {"page": 4, "text": "The Resolution Plans received from Dipesh Silk Mills Private Limited, "
                             "Siddhachal Fab Private Limited and Siddhivinayak Steels. The CoC considered them."},
        {"page": 5, "text": "The Resolution Plan provides for payment of Rs. 3,96,00,000/- to stakeholders."},
        {"page": 6, "text": "The Fair Value of the Corporate Debtor's assets is mentioned in Form H as "
                             "Rs.3,69,23,214/- and the Liquidation Value of the Corporate Debtor's assets "
                             "is Rs.2,92,61,800/-."},
        {"page": 7, "text": "As on 10.04.2025, the list of Corporate Debtor's creditors based on claims received: "
                             "\n1. Secured Financial Creditors 10,00,93,133/- 10,00,93,133/- 1"
                             "\n2. Unsecured Financial Creditors 19,00,30,356/- 19,00,30,356/- 1"
                             "\n3. Operational Creditors (Government Dues) 84,48,002/- 59,14,786/- 3"
                             "\n4. Operational Creditors (other than Workmen) 2,12,60,480/- 0.00/- 1"
                             "\nTOTAL 31,98,31,971/- 29,60,38,275/- 6"},
        {"page": 8, "text": "The Corporate Debtor is engaged in manufacturing of garments and textiles."},
    ]
    record, report = extract_case({"pages": pages}, "full-order")
    assert record["professional"]["value"] == "Krishna Chamadia"
    assert record["applicant"]["value"] == "Siddhachal Fab Private Limited"
    assert record["admission_date"]["value"] == "2024-11-14"
    assert record["industry"]["value"] == "manufacturing of garments and textiles"
    assert record["fair_value"]["value"] == "36923214.00"
    assert record["liquidation_value"]["value"] == "29261800.00"
    assert record["financial_creditors"][0]["value"] == "Axis Bank Limited"
    assert len(record["plan_submitters"]) == 3
    assert len(record["claims"]) == 4
    assert all(claim["scope"] == "category_total" and claim["plan_amount"] is None
               for claim in record["claims"])
    assert sum(int(float(claim["claimed"])) for claim in record["claims"]) == 319831971
    assert sum(int(float(claim["admitted"])) for claim in record["claims"]) == 296038275
    assert record["plans"][0]["amount"] == "39600000.00"
    assert record["plans"][0]["admitted"] is None
    assert report["evidence"]



def test_extended_order_facts_and_matched_haircut():
    pages = [
        {"page": 1, "text": "IN THE NATIONAL COMPANY LAW TRIBUNAL MUMBAI BENCH-II\n"
                             "CP (IB) No. 1069/MB/2023\n"
                             "Guruanand Silk Mills Private Limited ...Corporate Debtor\n"
                             "Mr. Krishna Chamadia [Registration No. IBBI/001] Resolution Professional of "
                             "Guruanand Silk Mills Private Limited\n"
                             "CORAM: HONBLE SHRI ASHISH KALIA, MEMBER (JUDICIAL)\n"
                             "HONBLE SHRI BANWARI LAL MEENA, MEMBER (TECHNICAL)"},
        {"page": 2, "text": "Claims received amounting to Rs. 31,98,31,971/-. "
                             "The CoC was constituted with Axis Bank Limited as the Sole Financial Creditor "
                             "having 100% voting rights."},
        {"page": 3, "text": "One of the PRAs, namely Siddhivinayak Steel, expressed its intention to "
                             "withdraw from the resolution process."},
        {"page": 4, "text": "The Resolution Applicant, Siddhachal Fab Private Limited incorporated on "
                             "22.12.2012, is a family-owned textile weaving factory based in Bhiwandi, Maharashtra. "
                             "Harsh Haria, Director of the SRA, signed the affidavit."},
        {"page": 5, "text": "Fair Value Liquidation Value Valuer 1 "
                             "1 Land and Building (L&B) Valuer 1 3,44,67,000/- 2,77,67,700/- "
                             "Average L&B 3,50,80,448/- 2,82,01,019/- "
                             "2 Plant & Machinery (P&M) Valuer 1 15,92,516/- 7,96,528/- "
                             "Average (P&M) 15,64,240/- 7,82,255/-"},
        {"page": 6, "text": "TOTAL 31,98,31,971/- 29,60,38,275/- 6"},
        {"page": 7, "text": "The summary of the payments to be made under the Resolution Plan is as follows: "
                             "Sr. No. Particulars Amount Admitted (Rs.) Proposed Payments (Rs.) "
                             "1. CIRP Cost - 28,00,000/- "
                             "2. Secured Financial Creditors 10,00,93,133/- 3,43,59,166/- "
                             "3. Unsecured Financial Creditors 19,00,30,356/- 19,00,303/- "
                             "4. Operational Creditors (Government Dues) 59,14,786/- 59,147/- "
                             "5. Operational Creditors (Government Dues EPFO) - 4,81,384/- "
                             "Total Plan Value - 3,96,00,000/-"},
    ]
    record, report = extract_case({"pages": pages}, "order-pdf")
    assert record["case_number"]["value"] == "CP (IB) No. 1069/MB/2023"
    assert record["professional"]["value"] == "Krishna Chamadia"
    assert record["total_received_claims"]["value"] == "319831971.00"
    assert record["total_admitted_claims"]["value"] == "296038275.00"
    assert record["coc_voting_share"]["value"] == "Axis Bank Limited: 100%"
    assert record["withdrawn_submitters"][0]["value"] == "Siddhivinayak Steel"
    assert len(record["judges"]) == 2
    assert record["applicant_directors"][0]["value"] == "Harsh Haria"
    assert record["previous_directors"] == []
    assert len(record["asset_values"]) == 4
    assert len(record["plan_payments"]) == 5
    assert record["plan_payments"][0]["recipient"] == "CIRP Cost"
    assert record["plan_payments"][0]["page"] == 7
    assert report["evidence"]



def test_existing_draft_gains_only_missing_payment_amounts():
    current, _ = extract_case(
        {"pages": [{"page": 1, "text": "Creditor: Bank A; Type: secured financial; "
                                         "Claimed: Rs 100; Admitted: Rs 100"}]}, "old-pdf")
    proposed, _ = extract_case(
        {"pages": [{"page": 1, "text": "Creditor: Bank A; Type: secured financial; "
                                         "Claimed: Rs 100; Admitted: Rs 100; Plan amount: Rs 20"}]}, "new-pdf")
    updated = fill_missing(current, proposed)
    assert updated["claims"][0]["plan_amount"] == "20.00"
    assert updated["claims"][0]["document_id"] == "old-pdf"
    assert fill_missing(updated, proposed)["claims"] == updated["claims"]
    current["claims"][0]["plan_amount"] = "30.00"
    assert fill_missing(current, proposed)["claims"][0]["plan_amount"] == "30.00"
