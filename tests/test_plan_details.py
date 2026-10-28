import json
from decimal import Decimal
from pathlib import Path

from backend.case_extraction import extract_case, money


def source():
    return json.loads((Path(__file__).parent/'fixtures/arihant-full-fields.json').read_text())


def test_rejected_claims_shared_allocations_and_proposed_ownership_have_source_pages():
    record, report = extract_case(source(), 'arihant')
    claims = {row['creditor']: row for row in record['claims']}
    assert len(claims) == 9
    assert len(record['financial_creditors']) == 6
    for name, amount in [('Vishal Surana HUF','29000490.00'), ('Vaishali Surana','140485085.00'), ('Sanskar Surana','12754608.00')]:
        row = claims[name]
        assert row['claimed'] == amount
        assert row['admitted'] == '0.00'
        assert row['plan_amount'] == '0.00'
        assert row['actual_paid'] is None
        assert row['page'] == 16
    assert claims['Government Dues']['admitted'] == '202139948.00'
    assert claims['Other Operational Creditors']['admitted'] == '36205600.00'
    assert claims['Workmen/Employees']['admitted'] == '0.00'
    assert all(claims[k]['plan_amount'] is None for k in ('Government Dues','Other Operational Creditors','Workmen/Employees'))
    payments = {p['recipient']:p for p in record['plan_payments']}
    assert len(payments) == 6
    assert payments['CIRP costs']['amount'] == '6500000.00'
    assert payments['CIRP costs']['admitted'] is None  # Estimated costs are not admitted claims.
    assert payments['CIRP costs']['page'] == 14
    assert payments['Employees State Insurance Corporation']['amount'] == '67010.00'
    assert payments['Other operational creditors (combined)']['amount'] == '1132990.00'
    assert sum(Decimal(p['amount']) for p in payments.values()) == Decimal('430200000.00')
    assert 'Indian textile and retail sector' in record['applicant_business']['value']
    assert record['applicant_business']['page'] == 11
    assert record['proposed_owners'][0]['value'] == 'POTHYS RETAIL PRIVATE LIMITED'
    assert record['proposed_owners'][0]['page'] == 23
    assert record['current_owners'] == []
    assert record['subsequent_owners'] == []
    assert any('estimated' in warning for warning in report['warnings'])


def test_mismatching_rejected_amounts_are_not_used_to_infer_zero_admission():
    extraction = source()
    extraction['pages'][15]['ocr_layout_text'] = extraction['pages'][15]['ocr_layout_text'].replace('2,90,00,490.00','2,90,00,491.00',1)
    record, _ = extract_case(extraction, 'mismatch')
    assert not any(c['category'] == 'unsecured_financial' for c in record['claims'])


def test_form_money_labels_accept_pdf_spacing_and_rupee_suffix():
    assert money('Rs. 67 ,010/-') == '67010.00'
    assert money('INR 8,5 5,950.00') == '855950.00'
    assert money('Rs 12 crore/-') == '120000000.00'
    assert money('Rs 12 crore 5') is None
    assert money('Nil') == '0.00'
    assert money('Zero') == '0.00'
