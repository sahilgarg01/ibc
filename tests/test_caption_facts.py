from backend.case_extraction import extract_case


def test_attendance_sheet_and_narrative_fields():
    pages = [
        '''IN THE NATIONAL COMPANY LAW TRIBUNAL
DIVISION BENCH (COURT– I) CHENNAI
PRESENT: HON’BLE SHRI. SANJIV JAIN, MEMBER (JUDICIAL)
HON’BLE SHRI. VENKATARAMAN SUBRAMANIAM, MEMBER (TECHNICAL )
MAIN PETITION NUMBER : CP(IB)/265(CHE)/2024
Date: 13.10.2026''',
        '''Shruti Lohia
Resolution Professional for Arihant Retail Private Limited
The Successful Resolution Applicant (SRA) viz., POTHYS RETAIL PRIVATE LIMITED.
The Corporate Debtor is a private limited company incorporated on 21.05.1996 having its registered office at No.29, Chennai, India – 600021. The Corporate Debtor was involved in retail textile business.
The Petition was admitted by this Tribunal vide an order dated 02.05.2025 and Sivakumar Amarendran (erstwhile IRP) was appointed as the Interim Resolution Professional.
The total Resolution Plan value is Rs. 43,02,00,000/.
Fair Value of the Corporate Debtor is INR 49,57,26,750/- and Liquidation Value is INR 38,47,53,313/-.
The Resolution Plan is hereby approved.''',
    ]
    record, _ = extract_case({'pages': [{'page': i+1, 'text': text} for i, text in enumerate(pages)]}, 'doc')
    for field, expected in {'case_number': 'CP(IB)/265(CHE)/2024', 'order_date': '2026-10-13',
                            'admission_date': '2025-05-02', 'professional': 'Shruti Lohia',
                            'irp': 'Sivakumar Amarendran', 'industry': 'retail textile business',
                            'fair_value': '495726750.00', 'liquidation_value': '384753313.00',
                            'applicant': 'POTHYS RETAIL PRIVATE LIMITED'}.items():
        assert record[field]['value'] == expected
    assert 'CHENNAI' in record['tribunal']['value']
    assert len(record['judges']) == 2
    assert record['plans'][0]['amount'] == '430200000.00'
    assert record['registered_office']['page'] == 2


def test_legal_entitlement_and_role_are_not_applicant_profile_or_name():
    text = '''Approval of resolution plan submitted by the Successful Resolution Applicant (SRA) viz., Buyer Private Limited.
The Resolution Applicant is entitled to carry forward losses, such benefit may be claimed before the appropriate authority.'''
    record, _ = extract_case({'pages': [{'page': 1, 'text': text}]}, 'doc')
    assert record['applicant']['value'] == 'Buyer Private Limited'
    assert record['applicant_business'] is None
    assert [item['value'] for item in record['plan_submitters']] == ['Buyer Private Limited']


def test_complete_ocr_category_row_preserves_column_meanings():
    layout = '''Total claims admitted (In Rs.) payable as per Resolution Plan
Secured Financial Creditors         1,34,55,73,174.71  42.25.00.000.00      31.40%
Operational Creditors
-GovernmentDues           20.21.39.948.00
-Workmen/Employees                   Nil    12.00.000.00      0.50%'''
    record, _ = extract_case({'pages': [{'page': 1, 'text': layout, 'layout_text': layout}]}, 'doc')
    assert len(record['claims']) == 1
    claim = record['claims'][0]
    assert claim['admitted'] == '1345573174.71'
    assert claim['plan_amount'] == '422500000.00'
    assert claim['claimed'] is None
    assert record['total_admitted_claims'] is None
    assert record['plan_payments'][0]['amount'] == '422500000.00'
