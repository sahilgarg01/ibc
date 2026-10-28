import json
from pathlib import Path

from backend.case_extraction import extract_case


def read(name):
    return json.loads((Path(__file__).parent/'fixtures'/(name+'.json')).read_text())


def test_screenshot_order_checks_every_field_and_fills_all_stated_facts():
    from backend.models import CaseRecord
    record, report = extract_case(read('marks-pryor-liquidation'), 'future-liquidation')
    expected = {
        'company': ('Marks Pryor Marking Technology Private Limited', 1),
        'case_number': ('C.P.(IB)/600/(MB)/2023', 1),
        'professional': ('Mandar S. Wagh', 1),
        'irp': ('Mandar S. Wagh', 2),
        'liquidator': ('Atul Narayan Naik', 7),
        'order_date': ('2026-10-16', 1),
        'coc_voting_share': ('100%', 3),
    }
    for field, (value, page) in expected.items():
        assert (record[field]['value'], record[field]['page']) == (value, page), field
    assert record['tribunal']['value'] == 'NCLT Mumbai'
    assert record['financial_creditors'][0]['value'] == 'Bank of Maharashtra'
    assert [v['value'] for v in record['judges']] == ['Anil Raj Chellan', 'K. R. Saji Kumar']
    assert record['outcome'] == 'liquidation_ordered'
    assert record['outcome_evidence']['page'] == 7
    claims = {v['creditor']: v for v in record['claims']}
    assert claims['Bank of Maharashtra']['claimed'] == '110626372.57'
    assert claims['Operational Creditors']['claimed'] == '245109797.00'
    assert all(row['admitted'] is None and row['plan_amount'] is None and
               row['actual_paid'] is None for row in claims.values())
    # Audit the complete schema, including the fields this source never states.
    present = set(expected) | {'tribunal', 'financial_creditors', 'judges', 'outcome', 'outcome_evidence', 'claims'}
    not_applicable = {'resolution_date', 'applicant', 'applicant_cin', 'applicant_incorporated',
                      'applicant_business', 'applicant_location', 'applicant_directors', 'plans', 'plan_payments'}
    fields = set(CaseRecord.model_fields) - {'notes'}
    assert set(report['field_review']) == fields
    for field in fields:
        review = report['field_review'][field]
        assert review['reason']
        if field in present:
            assert record[field] and review['status'] == 'extracted', field
        elif field in not_applicable:
            assert not record[field] and review['status'] == 'not_applicable', field
            assert review['sources'][0]['page'] == 4
        else:
            assert not record[field] and review['status'] == 'not_found', field
    assert record['admission_date'] is None  # Public-announcement date is not admission.
    assert record['resolution_date'] is None  # Liquidation date is not plan approval.
    assert record['registered_office'] is None  # Liquidator's address is not the debtor's.
    assert record['fair_value'] is None  # Preliminary expenses are not valuations.
    assert report['field_review']['fair_value']['sources'][0]['page'] == 3
    assert 'valuers' in report['field_review']['fair_value']['reason']


def test_missing_plan_is_not_assumed_from_liquidation_outcome_alone():
    text = '''NATIONAL COMPANY LAW TRIBUNAL
    Corporate debtor: Example Limited
    The Corporate Debtor, Example Limited, is ordered to be liquidated.'''
    record, report = extract_case({'pages': [{'page': 1, 'text': text}]}, 'new')
    assert record['outcome'] == 'liquidation_ordered'
    assert report['field_review']['applicant']['status'] == 'not_found'
    assert report['field_review']['plans']['status'] == 'not_found'


def test_shree_badri_role_scoped_identity_and_appointments():
    record, _ = extract_case(read('shree-badri-caption'), 'shree')
    expected = {
        'company': ('Shree Badri Kedar Udyog Private Limited', 1),
        'cin': ('U51909JH2011PTC014715', 1),
        'registered_office': ('Shop NO. 206, 2nd Floor, Samridhi Square, Kishorganj, Ranchi – 834 001', 1),
        'industry': ('trading of fabrics and garments', 5),
        'professional': ('Rachna Jhunjhunwala', 1),
        'irp': ('Rachna Jhunjhunwala', 3),
        'liquidator': ('Udit Agarwal', 8),
        'admission_date': ('2026-02-05', 3),
        'order_date': ('2026-10-13', 1),
        'coc_voting_share': ('100%', 3),
    }
    for field, (value, page) in expected.items():
        assert record[field]['value'] == value, field
        assert record[field]['page'] == page, field
    assert record['tribunal']['value'] == 'NCLT DIVISION BENCH (Court-I), KOLKATA'
    assert [v['value'] for v in record['judges']] == ['Bidisha Banerjee', 'Rekha Kantilal Shah']
    assert record['financial_creditors'][0]['value'] == 'Bank of India'
    assert 'Star House' not in record['registered_office']['value']
    assert 'Siddha Weston' not in record['registered_office']['value']


def test_atharva_caption_and_late_operative_identity():
    record, _ = extract_case(read('atharva-caption'), 'atharva')
    assert record['company']['value'] == 'Atharva Auto Logistics Private limited'
    assert record['registered_office']['value'] == 'Flat No. 2, SN-199/204/206 Siddhant Classic, Viman Nagar, Pune - 411014'
    assert record['cin']['value'] == 'U63030PN2014PTC152640'
    assert record['cin']['page'] == 35
    assert record['admission_date']['value'] == '2022-02-18'
    assert record['irp']['value'] == 'Uday Kumar Bhaskar Bhatt'
    assert record['liquidator']['value'] == 'Vinit Gangwal'
    assert record['liquidator']['page'] == 35
    assert record['applicant']['value'] == 'Suyog Satish Pachange'
    assert len(record['applicant']['value'].split()) == 3


def test_sri_krishna_operative_appointment_beats_rejected_proposals():
    record, _ = extract_case(read('sri-krishna-caption'), 'krishna')
    assert 'Sri Krishna Stockist' in record['company']['value']
    assert record['tribunal']['value'] == 'NCLT Amaravati'
    assert record['case_number']['value'].replace(' ', '') == 'CP(IB)/12/7/AMR/2025'
    assert record['irp']['value'] == 'Chillale Rajesh'
    assert record['liquidator']['value'] == 'Mannava Divakara Sarma'
    assert record['liquidator']['page'] == 56
    assert {v['value'] for v in record['judges']} == {'Kishore Vemulapalli', 'Umesh Kumar Shukla'}


def test_vaijanath_merged_ocr_claims_reconcile_and_do_not_become_plan_payments():
    record, _ = extract_case(read('vaijanath-claims'), 'vaijanath')
    claims = {row['creditor']: row for row in record['claims']}
    assert set(claims) == {'Kamal Metal Corporation', 'Income Tax Department'}
    assert claims['Kamal Metal Corporation']['claimed'] == '17864899.00'
    assert claims['Income Tax Department']['admitted'] == '1608250.00'
    assert claims['Income Tax Department']['category'] == 'government'
    assert record['total_received_claims']['value'] == '19473149.00'
    assert record['total_admitted_claims']['value'] == '19473149.00'
    assert record['total_admitted_claims']['page'] == 4
    assert all(row['page'] == 4 and row['plan_amount'] is None for row in claims.values())
    assert record['plan_payments'] == []
    assert record['liquidator']['value'] == 'Neehal Mahamulal Pathan'


def test_ocr_claims_with_unreconciled_total_stay_unknown():
    extraction = read('vaijanath-claims')
    for page in extraction['pages']:
        if page['page'] == 4:
            page['ocr_layout_text'] = page['ocr_layout_text'].replace('1,94,73,149.00', '1,94,73,150.00')
    record, _ = extract_case(extraction, 'vaijanath')
    assert not record['claims']
    assert record['total_admitted_claims'] is None


def test_arihant_latest_coc_joins_reconciled_named_payment_schedule():
    from decimal import Decimal
    record, report = extract_case(read('arihant-creditors'), 'arihant')
    claims = {row['creditor']: row for row in record['claims'] if row['category'] == 'secured_financial'}
    assert len(claims) == 3
    lic = claims['LIC Housing Finance Ltd.']
    assert lic['admitted'] == '937248285.00'  # Revised CoC, not its earlier 502442002.
    assert lic['claimed'] == '949995862.00'
    assert lic['plan_amount'] == '415000000.00'
    assert claims['Axis Bank']['plan_amount'] == '3544681.00'
    assert claims['Karuru Vysya Bank']['plan_amount'] == '3955319.00'
    assert all(row['scope'] == 'individual' and row['page'] == 5 for row in claims.values())
    assert len(record['financial_creditors']) == 3
    assert len(record['plan_payments']) == 3
    assert all(item['page'] == 15 for item in record['plan_payments'])
    assert sum(Decimal(item['amount']) for item in record['plan_payments']) == Decimal('422500000')
    assert any('rounded' in warning for warning in report['warnings'])


def test_unreconciled_named_schedule_does_not_replace_stated_category_claim():
    extraction = read('arihant-creditors')
    for page in extraction['pages']:
        if page['page'] == 15:
            page['ocr_layout_text'] = page['ocr_layout_text'].replace('42.25.00.000', '42.26.00.000')
    record, _ = extract_case(extraction, 'arihant')
    claims = [c for c in record['claims'] if c['category'] == 'secured_financial']
    assert len(claims) == 1
    assert claims[0]['scope'] == 'category_total'


def test_sri_krishna_crore_claims_keep_principal_interest_and_verification_separate():
    record, report = extract_case(read('sri-krishna-caption'), 'krishna')
    claims = {r['creditor']: r for r in record['claims']}
    assert set(claims) == {'Canara Bank', 'IFCI Limited', 'IDBI Bank Limited', 'Income Tax Department'}
    assert claims['Canara Bank']['claimed'] == '6651100000.00'
    assert claims['IFCI Limited']['admitted'] == '1809400000.00'
    assert claims['IDBI Bank Limited']['admitted'] == '1070900000.00'
    assert claims['Income Tax Department']['claimed'] == '228300000.00'
    assert claims['Income Tax Department']['admitted'] == '18700000.00'
    assert all(c['plan_amount'] is None and c['actual_paid'] is None and c['page'] == 10 for c in claims.values())
    assert record['total_received_claims']['value'] == '9759600000.00'  # Printed total; not silently corrected.
    assert record['total_admitted_claims']['value'] == '9550100000.00'
    assert record['plan_payments'] == []
    assert len(record['financial_creditors']) == 3
    assert any('printed claims abstract total differs' in w for w in report['warnings'])


def test_financial_analysis_is_not_mapped_as_a_claim_abstract():
    extraction = read('sri-krishna-caption')
    page = extraction['pages'][9]
    page['ocr_layout_text'] = page['ocr_layout_text'].replace('abstract of claims received', 'financial analysis')
    record, _ = extract_case(extraction, 'krishna')
    assert record['claims'] == []
    assert record['total_admitted_claims'] is None


def test_unsecured_named_schedule_is_not_classified_as_secured():
    import re
    extraction = read('arihant-creditors')
    for page in extraction['pages']:
        for field in ('text', 'layout_text', 'ocr_layout_text'):
            if field in page:
                page[field] = re.sub(r'\bSecured\b', 'Unsecured', page[field], flags=re.I)
    record, _ = extract_case(extraction, 'unsecured')
    claims = [c for c in record['claims'] if c['category'] == 'unsecured_financial']
    assert len(claims) == 3
    assert all(c['scope'] == 'individual' for c in claims)
    assert len(record['plan_payments']) == 3
