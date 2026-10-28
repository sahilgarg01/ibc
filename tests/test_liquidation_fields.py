"""Regression coverage for narrative roles and continued INR creditor tables."""
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from backend.case_extraction import extract_case
from backend.extraction import READER_VERSION
from backend.main import create_app
from backend.models import CaseRecord, metrics


def liquidation_extraction():
    pages = [
        {'page': 1, 'text': 'NATIONAL COMPANY LAW TRIBUNAL\nCOURT-V, MUMBAI BENCH\nOrder Delivered on 22.10.2026'},
        {'page': 2, 'text': 'CS (Dr.) Rajas Shreeram Bodas\nResolution Professional of M/s Shitole Tissue Culture Private Limited'},
        {'page': 3, 'text': 'Resolution Professional of M/s Shitole Tissue Culture Private Limited (“Corporate Debtor”).'},
        {'page': 4, 'text': '''The Corporate Insolvency Resolution process (“CIRP”) of the M/s Shitole Tissue Culture Private Limited was initiated by this Tribunal vide Order dated 20.01.2025.
Initially, CS Anagha Anasingaraju was appointed as Interim Resolution Professional.''',
         'tables': [[['Sr.No', 'Name of\nFinancial\nCreditors', 'Amount (INR)\nClaimed by\nthe Financial\nCreditors', 'Amount (INR)\nadmitted on\nProvisional\nBasis', 'Voting\nPercentage'],
                     ['1', 'Janaseva\nSahakari\nBank\nLimited', '8,41,52,298.80', '8,41,52,298.80', '100%']]]},
        {'page': 5, 'text': 'Two registered valuers valued the plant and machinery, and their reports are summarised below:',
         'tables': [[['', 'Total', '8,41,52,298.80', '8,41,52,298.80', '100.00%']],
                    [['Particulars', 'Fair Market\nValue', 'Liquidation\nValue', 'Average'],
                     ['Valuer 1\nVr. Er. Tembe Pravin Vasant', '2,85,920', '2,11,012', '2,48,466'],
                     ['Valuer 2\nAnkit Gupta', '2,78,936', '2,23,149', '2,51,042']]]},
        {'page': 6, 'text': '''No resolution plan was received. CS Anagha Anasingaraju is appointed as the Liquidator of the Corporate Debtor.
The Corporate Debtor, M/s Shitole Tissue Culture Private Limited is directed to be liquidated.'''},
    ]
    for page in pages:
        page.setdefault('layout_text', page['text'])
        page.setdefault('tables', [])
    return {'reader_version': READER_VERSION, 'pages': pages, 'warnings': [], 'candidates': [], 'subject': None}


def assert_fields(record):
    for field, expected in {
        'company': 'Shitole Tissue Culture Private Limited',
        'tribunal': 'NCLT COURT-V, MUMBAI BENCH',
        'admission_date': '2025-01-20',
        'professional': 'Rajas Shreeram Bodas',
        'irp': 'Anagha Anasingaraju',
        'liquidator': 'Anagha Anasingaraju',
        'total_received_claims': '84152298.80',
        'total_admitted_claims': '84152298.80',
    }.items():
        assert record[field]['value'] == expected, field
        assert record[field]['document_id']
        assert 1 <= record[field]['page'] <= 6
    assert record['financial_creditors'][0]['value'] == 'Janaseva Sahakari Bank Limited'
    assert record['claims'][0]['claimed'] == '84152298.80'
    assert record['claims'][0]['admitted'] == '84152298.80'
    assert record['claims'][0]['plan_amount'] is None
    assert record['claims'][0]['page'] == 4
    assert record['applicant'] is None
    assert record['plans'] == []
    assert record['registered_office'] is None
    assert len(record['asset_values']) >= 1
    calculated = metrics(CaseRecord.model_validate(record))['asset_valuations'][0]
    assert calculated['asset'] == 'plant and machinery'
    assert calculated['fair_value'] == '282428.00'
    assert calculated['liquidation_value'] == '217080.50'


def test_liquidation_roles_claims_and_valuer_basis():
    record, report = extract_case(liquidation_extraction(), 'document')
    assert_fields(record)
    assert 'professional' not in report['missing_fields']
    assert 'claims' not in report['missing_fields']


def test_upload_fills_form_and_explicit_reextract_repairs_incomplete_reader(tmp_path, monkeypatch):
    import json
    import backend.main as main
    from backend.db import connection
    extraction = liquidation_extraction()
    monkeypatch.setattr(main, 'extract_pdf', lambda content: extraction.copy())
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/documents', files={'file': ('liquidation.pdf', buf.getvalue(), 'application/pdf')})
        assert response.status_code == 200, response.text
        ids = response.json()
        detail = client.get('/api/cases/' + ids['case_id']).json()
        assert_fields(detail['record'])
        # Saved cases remain unchanged on reads. An explicit retry still repairs
        # a failed reader even if the fact extraction version is current.
        with connection(tmp_path, database_url='') as db:
            damaged = json.loads(db.execute('SELECT extraction FROM documents WHERE id=?', (ids['document_id'],)).fetchone()[0])
            del damaged['pages'][3]['tables']
            db.execute('UPDATE documents SET extraction=? WHERE id=?', (json.dumps(damaged), ids['document_id']))
        calls = []
        def repaired(content):
            calls.append(content)
            return liquidation_extraction()
        monkeypatch.setattr(main, 'extract_pdf', repaired)
        detail = client.get('/api/cases/' + ids['case_id']).json()
        assert not calls
        response = client.post('/api/documents/' + ids['document_id'] + '/extract-facts')
        assert response.status_code == 200, response.text
        detail = client.get('/api/cases/' + ids['case_id']).json()
        assert calls
        assert_fields(detail['record'])


def test_hybrid_pdf_narratives_use_image_text_without_changing_cached_pages():
    extraction = {'pages': [{'page': 1,
        'text': 'Corporate debtor: Example Limited\n' + 'Readable header and background. ' * 30,
        'embedded_text': 'Corporate debtor: Example Limited',
        'ocr_text': '''The Corporate Debtor is primarily engaged in the business of textile manufacturing.
The Petition was admitted by this Tribunal vide an order dated 02.05.2025.
CS (Dr.) Rajas Shreeram Bodas Resolution Professional of M/s Example Limited.
CS Anagha Anasingaraju is appointed as the Liquidator of the Corporate Debtor.'''}]}
    import copy
    original = copy.deepcopy(extraction)
    record, _ = extract_case(extraction, 'hybrid')
    assert extraction == original
    assert record['company']['value'] == 'Example Limited'
    assert record['industry']['value'] == 'textile manufacturing'
    assert record['admission_date']['value'] == '2025-05-02'
    assert record['professional']['value'] == 'Rajas Shreeram Bodas'
    assert record['liquidator']['value'] == 'Anagha Anasingaraju'
    for field in ('industry', 'admission_date', 'professional', 'liquidator'):
        assert record[field]['page'] == 1


def test_image_claim_layout_is_used_when_embedded_layout_is_longer():
    text = 'Corporate debtor: Example Limited\n' + 'Readable narrative. ' * 30
    ocr = '''Total claims admitted (In Rs.) payable as per Resolution Plan
Secured Financial Creditors         1,34,55,73,174.71  42.25.00.000.00      31.40%'''
    record, _ = extract_case({'pages': [{'page': 1, 'text': text, 'layout_text': text,
        'ocr_text': ocr, 'ocr_layout_text': ocr}]}, 'hybrid')
    assert record['claims'][0]['admitted'] == '1345573174.71'
    assert record['claims'][0]['plan_amount'] == '422500000.00'
    assert record['claims'][0]['page'] == 1


def test_explicit_form_lists_and_actual_payments_keep_distinct_roles():
    text = '''Corporate debtor: Example Limited
Proposed owner: Future Holdings Limited; CIN: U12345MH2020PTC123456
Proposed director: Mira Patel; DIN: 12345678
Resolution applicant director: Arun Shah; DIN: 23456789
Financial creditor: First Bank Limited
Plan submitter: Future Holdings Limited
Withdrawn plan submitter: Former Buyer Limited
Judge: Justice Kavita Rao
Creditor: First Bank Limited; Type: secured financial; Claimed: Rs 100; Admitted: Rs 90; Plan amount: Rs 50; Actual paid: Rs 20
'''
    record, _ = extract_case({'pages': [{'page': 1, 'text': text}]}, 'lists')
    assert record['proposed_owners'][0]['cin'] == 'U12345MH2020PTC123456'
    assert record['proposed_directors'][0]['din'] == '12345678'
    assert record['applicant_directors'][0]['din'] == '23456789'
    assert record['financial_creditors'][0]['value'] == 'First Bank Limited'
    assert record['plan_submitters'][0]['value'] == 'Future Holdings Limited'
    assert record['withdrawn_submitters'][0]['value'] == 'Former Buyer Limited'
    assert record['judges'][0]['value'] == 'Justice Kavita Rao'
    assert record['claims'][0]['plan_amount'] == '50.00'
    assert record['claims'][0]['actual_paid'] == '20.00'
    assert record['current_owners'] == []
    assert record['current_directors'] == []


def test_currency_in_claim_headers_does_not_hide_amount_role():
    from backend.field_rules import column_role
    assert column_role('Amount (Rs.) Claimed by Creditors') == 'claimed'
    assert column_role('Amount (INR in lakhs) admitted') == 'admitted'
    assert column_role('Amount (Rupees) Admitted') == 'admitted'


def test_field_values_on_next_line_are_not_lost_or_taken_from_next_label():
    record, _ = extract_case({'pages': [{'page': 2, 'text': '''Name of corporate debtor:
Example Private Limited
Industry:
Textile manufacturing
Name of resolution professional:
Mira Patel
Date of initiation of CIRP:
02.05.2025
Date of resolution plan approval:
03.06.2026
Fair value:
Rs 10 crore
Liquidation value:
Rs 8 crore
Registered office:
Industry:
Textile manufacturing'''}]}, 'wrapped')
    assert record['company']['value'] == 'Example Private Limited'
    assert record['industry']['value'] == 'Textile manufacturing'
    assert record['professional']['value'] == 'Mira Patel'
    assert record['admission_date']['value'] == '2025-05-02'
    assert record['resolution_date']['value'] == '2026-06-03'
    assert record['fair_value']['value'] == '100000000.00'
    assert record['liquidation_value']['value'] == '80000000.00'
    assert record['registered_office'] is None
    assert record['professional']['page'] == 2


def test_debtor_registered_address_in_professional_caption_is_read_to_pin():
    text = '''CS (Dr.) Rajas Shreeram Bodas
Resolution Professional of M/s Shitole Tissue Culture Private Limited
Having its registered address at:
Survey No. 997/18, Satsang Society,
Suraksha Apartments, Navi Peth, Near
Vaikunth, Pune, Maharashtra – 411 030
… Applicant/Resolution Professional'''
    record, _ = extract_case({'pages': [{'page': 2, 'text': text}]}, 'address')
    assert record['registered_office']['value'] == ('Survey No. 997/18, Satsang Society, '
        'Suraksha Apartments, Navi Peth, Near Vaikunth, Pune, Maharashtra – 411 030')
    assert record['registered_office']['page'] == 2
    # Personal service addresses must not become the corporate debtor's office.
    record, _ = extract_case({'pages': [{'page': 2, 'text': text.replace('its registered', 'his registered')}]}, 'address')
    assert record['registered_office'] is None
