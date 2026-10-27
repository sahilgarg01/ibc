import json
from pathlib import Path

from backend.case_extraction import extract_case
from backend.models import CaseRecord, metrics


def test_maharashtra_order_recovers_identity_valuations_and_claims():
    extraction = json.loads((Path(__file__).parent / 'fixtures/maharashtra-bio-fertilisers.json').read_text())
    record, report = extract_case(extraction, 'maharashtra-order')
    assert record['company']['value'] == 'Maharashtra Bio Fertilisers (India) Private Limited'
    assert record['cin']['value'] == 'U24120MH2003PTC140320'
    assert record['registered_office']['value'].endswith('413531')
    assert record['professional']['value'] == 'MVK IPE LLP'
    assert record['irp']['value'] == 'Arun Nandlal Agrawal'
    assert record['applicant']['value'] == 'Nilesh Hemraj Thakkar'
    assert record['plan_submitters'][0]['value'] == 'Nilesh Hemraj Thakkar'
    assert record['admission_date']['value'] == '2024-11-21'
    assert record['order_date']['value'] == '2026-08-20'
    assert record['fair_value']['value'] == '240028.50'
    assert record['liquidation_value']['value'] == '162868.00'
    assert len(record['claims']) == 3
    assert record['claims'][-1]['admitted'] == '208907823.00'
    assert record['plans'][0]['amount'] == '190000000.00'
    assert len(record['plan_payments']) == 3
    result = metrics(CaseRecord.model_validate(record))
    assert result['plan_haircut_percent'] == '69.45'
    assert result['claim_received_matches_total'] is True
    assert result['claim_admitted_matches_total'] is True
    assert result['asset_fair_matches_total'] is True
    assert result['asset_liquidation_matches_total'] is True
    assert result['plan_payments_match_total'] is True
    assert 'claims' in report['populated_fields']
    # A proposed equity allotment does not establish implemented ownership.
    assert record['current_owners'] == []
    assert all(c['actual_paid'] is None for c in record['claims'])


def test_unrelated_or_ambiguous_table_is_not_a_claim_table():
    extraction = {'pages': [{'page': 1, 'text': 'A general table',
        'tables': [[['Name', 'Amount'], ['Bank A', '123456']]]}]}
    record, _ = extract_case(extraction, 'unrelated')
    assert not record['claims']
    assert not record['plans']


def test_interim_rp_is_not_mistaken_for_current_rp():
    extraction = {'pages': [{'page': 1, 'text':
        'Mr. Arun Nandlal Agrawal was appointed as the Interim Resolution Professional.'}]}
    record, _ = extract_case(extraction, 'irp-only')
    assert record['irp']['value'] == 'Arun Nandlal Agrawal'
    assert record['professional'] is None


def test_iskrupa_flexible_headers_and_valuation_cells_across_pages():
    extraction = json.loads((Path(__file__).parent / 'fixtures/iskrupa-mall.json').read_text())
    record, _ = extract_case(extraction, 'iskrupa')
    assert record['company']['value'] == 'Iskrupa Mall Management Company Private Limited'
    assert record['case_number']['value'] == 'C.P. (IB)/839(MB)2022'
    assert record['order_date']['value'] == '2026-08-18'
    assert record['professional']['value'] == 'Aegis Resolution Services Private Limited'
    assert record['applicant']['value'] == 'Marutee Finsec'
    assert record['coc_voting_share']['value'] == '83.05%'
    assert record['fair_value']['value'] == '35664000.00'
    assert record['liquidation_value']['value'] == '27482000.00'
    assert record['asset_values'][0]['basis'] == 'Average'
    assert record['asset_values'][0]['fair_value_page'] == 14
    assert record['asset_values'][0]['liquidation_value_page'] == 15
    assert record['total_received_claims']['value'] == '2070222436.00'
    assert record['total_admitted_claims']['value'] == '1458321982.00'
    assert len(record['plan_payments']) == 5
    assert len(record['claims']) == 5
    assert record['claims'][0]['claimed'] == '1028200505.00'
    assert record['claims'][0]['actual_paid'] is None
    assert record['current_owners'] == [] and record['current_directors'] == []
    result = metrics(CaseRecord.model_validate(record))
    assert result['plan_payments_match_total'] is True
    assert result['claim_received_matches_total'] is True
    assert result['plan_haircut_percent'] == '99.68'


def test_reordered_claim_columns_share_the_same_mapping():
    extraction = {'pages': [{'page': 1, 'text': 'Amounts in INR', 'tables': [[
        ['Stakeholder Type', 'Proposed payment', 'Admitted amount', 'Claimed amount'],
        ['SECURED FINANCIAL CREDITORS', '', '', ''],
        ['Example Bank', '25.00', '100.00', '125.00'],
        ['Total', '25.00', '100.00', '125.00'],
    ]]}]}
    record, _ = extract_case(extraction, 'different-template')
    assert record['claims'][0]['claimed'] == '125.00'
    assert record['claims'][0]['admitted'] == '100.00'
    assert record['claims'][0]['plan_amount'] == '25.00'
    assert record['plan_payments'][0]['amount'] == '25.00'


def test_proposed_allotment_does_not_become_actual_owner():
    extraction = {'pages': [{'page': 1, 'text':
        'The Corporate Debtor shall issue and allot equity shares to Mr. Ravi Shah, Resolution Applicant, in consideration of new funds.'}]}
    record, _ = extract_case(extraction, 'proposal')
    assert record['proposed_owners'][0]['value'] == 'Ravi Shah'
    assert record['current_owners'] == []
    assert record['subsequent_owners'] == []


def test_continued_tables_keep_optional_columns_and_lakh_units():
    extraction = {'pages': [
        {'page': 1, 'text': 'Amounts in Lakhs', 'tables': []},
        {'page': 2, 'text': '', 'tables': [[
            ['Stakeholder Type', 'Amount Admitted', 'Proposed Payment'],
            ['Secured Financial Creditors', '100.00', '20.00'],
        ]]},
        {'page': 3, 'text': '', 'tables': [[
            ['Operational Creditors', '10.00', '1.00'],
            ['Total', '110.00', '21.00'],
        ]]},
    ]}
    record, _ = extract_case(extraction, 'continued')
    assert len(record['plan_payments']) == 2
    assert record['claims'][0]['claimed'] is None
    assert record['claims'][0]['admitted'] == '10000000.00'
    assert record['claims'][1]['plan_amount'] == '100000.00'


def test_arshiya_claims_only_parties_and_crore_valuations():
    extraction = json.loads((Path(__file__).parent / 'fixtures/arshiya-northern.json').read_text())
    record, _ = extract_case(extraction, 'arshiya')
    assert len(record['financial_creditors']) == 6
    assert record['financial_creditors'][0]['value'] == 'State Bank of India'
    assert record['claims'][0]['claimed'] == '1928300000.00'
    assert record['claims'][0]['admitted'] == '1928300000.00'
    assert record['claims'][0]['plan_amount'] is None
    assert record['fair_value']['value'] == '1640100000.00'
    assert record['liquidation_value']['value'] == '1189200000.00'
    assert len(record['asset_values']) == 3
    assert all(v['basis'] == 'Average' for v in record['asset_values'])
    assert record['asset_values'][0]['fair_value'] == '1510000000.00'
    assert all(v['fair_value_source_unit'] == 'crore' for v in record['asset_values'])
    assert len(record['plan_submitters']) == 6
    assert any(v['value'] == 'Parmjit Gandhi' for v in record['plan_submitters'])
    assert record['applicant']['value'] == 'Athena India Opportunities'
    assert record['applicant_location']['value'] == 'Mauritius'
    assert [v['value'] for v in record['judges']] == ['Vinay Goel', 'Charanjeet Singh Gulati']
    assert record['applicant_directors'] == []  # Subsidiaries are not directors.
    assert record['withdrawn_submitters'] == []  # Withdrawn appeals are not withdrawn plans.
    result = metrics(CaseRecord.model_validate(record))
    assert result['plan_payments_match_total'] is True


def test_full_inr_values_are_not_rescaled_or_marked_crore():
    extraction = {'pages': [{'page': 1, 'text': 'Amounts in INR', 'tables': [[
        ['Particulars', 'Amount in INR'],
        ['Fair Value', '1640100000'],
        ['Liquidation Value', '1189200000'],
    ]]}]}
    record, _ = extract_case(extraction, 'full-inr')
    assert record['fair_value']['value'] == '1640100000.00'
    assert record['asset_values'][0]['fair_value_source_unit'] == 'INR'
    assert record['asset_values'][0]['liquidation_value_source_unit'] == 'INR'


def test_multiline_nil_values_are_summed_without_concatenating_amounts():
    from backend.section_facts import table_money
    from backend.case_extraction import money
    assert table_money('97,800\nNIL\n92,400\n58,09,800', money) == '6000000.00'
    assert table_money('67,83,12,52\n6', money) == '678312526.00'


def test_valuation_unit_declaration_follows_repeated_header_words():
    extraction = {'pages': [{'page': 1, 'text':
        'The Registered Valuer submitted a report. (Amount in Rs. Crores) '
        'Registered Valuer Property Description Fair Value Liquidation Value', 'tables': [[
        ['Registered Valuer', 'Property Description', 'Fair Value', 'Liquidation Value'],
        ['Valuer A', 'Property A', '174.73', '74.02'],
        ['', 'Property B', '72.31', '51.02'],
        ['Total', '', '247.04', '125.04'],
    ]]}]}
    record, _ = extract_case(extraction, 'crore-property')
    assert record['fair_value']['value'] == '2470400000.00'
    assert record['liquidation_value']['value'] == '1250400000.00'
    assert [v['asset'] for v in record['asset_values']] == ['Property A', 'Property B']
    assert record['asset_values'][1]['fair_value_source_unit'] == 'crore'


def test_coram_on_second_page_reconstructs_wrapped_judge_names():
    text = '''Order pronounced on 03.09.2026
Coram:
Shri. Prabhat Kumar                  Shri Sushil Mahadeorao
                                    Kochey
Hon'ble Member (Technical)           Hon'ble Member (Judicial)
Appearances:
For the Applicant: Shri Advocate Name
ORDER
'''
    extraction = {'pages': [{'page': 1, 'text': 'GSTAAD HOTELS PRIVATE LIMITED'},
        {'page': 2, 'text': text, 'layout_text': text}]}
    record, report = extract_case(extraction, 'gstaad')
    assert [(judge['value'], judge['page']) for judge in record['judges']] == [
        ('Prabhat Kumar', 2), ('Sushil Mahadeorao Kochey', 2)]
    assert any(item['field'] == 'judges' and item['page'] == 2 for item in report['evidence'])


def test_case_library_download_metadata_and_pdf_attachment(tmp_path):
    from fastapi.testclient import TestClient
    from backend.main import ROOT, create_app
    from backend.db import connection
    from urllib.parse import quote
    filename = 'Order review \u2013 Mumbai.pdf'
    content = (ROOT / 'samples/indo-global-resolution-order.pdf').read_bytes()
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/documents', files={'file': (filename, content, 'application/pdf')})
        response.raise_for_status()
        doc_id, case_id = response.json()['document_id'], response.json()['case_id']
        listed = next(case for case in client.get('/api/cases').json() if case['id'] == case_id)
        assert listed['pdf_documents'] == [{'id': doc_id, 'filename': filename}]
        inline = client.get('/api/documents/' + doc_id + '/pdf')
        assert inline.content == content
        assert 'attachment' not in inline.headers.get('content-disposition', '')
        download = client.get('/api/documents/' + doc_id + '/pdf?download=true')
        assert download.content == content
        assert download.headers['content-type'] == 'application/pdf'
        assert 'attachment' in download.headers['content-disposition']
        assert quote(filename, safe='') in download.headers['content-disposition']
        with connection(tmp_path, database_url='') as db:
            db.execute('UPDATE documents SET content=NULL WHERE id=?', (doc_id,))
        disk_download = client.get('/api/documents/' + doc_id + '/pdf?download=true')
        assert disk_download.content == content
        assert 'attachment' in disk_download.headers['content-disposition']
        assert client.get('/api/documents/missing/pdf?download=true').status_code == 404


def test_automatic_refresh_corrects_suggestions_but_preserves_manual_saves(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.main import ROOT, create_app
    from backend.case_extraction import extract_case as original
    proposed_name = ['Corrected automatic company']
    def changed(extraction, document_id):
        record, report = original(extraction, document_id)
        record['company']['value'] = proposed_name[0]
        return record, report
    with TestClient(create_app(tmp_path)) as client:
        pdf = (ROOT / 'samples/indo-global-resolution-order.pdf').read_bytes()
        upload = client.post('/api/documents', files={'file': ('order.pdf', pdf, 'application/pdf')})
        upload.raise_for_status()
        doc_id, case_id = upload.json()['document_id'], upload.json()['case_id']
        monkeypatch.setattr('backend.main.extract_case', changed)
        client.post('/api/documents/' + doc_id + '/extract-facts').raise_for_status()
        first = client.get('/api/cases/' + case_id).json()
        assert first['record']['company']['value'] == proposed_name[0]
        client.post('/api/documents/' + doc_id + '/extract-facts').raise_for_status()
        assert client.get('/api/cases/' + case_id).json()['version'] == first['version']
        first['record']['company']['value'] = 'Manually verified company'
        client.put('/api/cases/' + case_id, json={'version': first['version'], 'record': first['record']}).raise_for_status()
        proposed_name[0] = 'A newer automatic suggestion'
        client.post('/api/documents/' + doc_id + '/extract-facts').raise_for_status()
        assert client.get('/api/cases/' + case_id).json()['record']['company']['value'] == 'Manually verified company'
