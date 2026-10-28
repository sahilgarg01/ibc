"""Regressions from the four independently reviewed final tribunal PDFs."""
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.case_extraction import VERSION, extract_case, money
from backend.main import create_app
from backend.models import CaseRecord, metrics
from backend.section_facts import table_money


def sources():
    return json.loads((Path(__file__).parent/'fixtures/attached-final-orders.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('source', sources(), ids=lambda source: source['id'])
def test_individually_reviewed_order_fields_and_calculations(source):
    record, report = extract_case(source['extraction'], 'new-document')
    model = CaseRecord.model_validate(record)
    calculated = metrics(model)
    assert set(report['field_review']) == set(CaseRecord.model_fields) - {'notes'}
    assert all(row['actual_paid'] is None for row in record['claims'])
    assert not record['withdrawn_submitters']  # Withdrawal of litigation is not a withdrawn plan.
    assert not record['applicant_directors']  # Subsidiaries and creditor directors are not applicant directors.
    assert record['applicant_cin'] is None
    for fact in report['evidence']:
        assert 1 <= fact['page'] <= len(source['extraction']['pages'])
    for asset in record['asset_values']:
        assert asset['fair_value_page'] and asset['liquidation_value_page']

    if source['id'] == 'atharva':
        assert record['cin']['value'] == 'U63030PN2014PTC152640'
        assert record['registered_office']['value'].startswith('Flat No. 2')
        assert record['industry']['value'] == 'yard management services'
        assert record['outcome'] == 'liquidation_ordered'
        assert record['resolution_date'] is None
        assert record['liquidator']['value'] == 'Vinit Gangwal'
        assert record['coc_voting_share']['value'] == '66.12%'
        assert record['plans'][0]['amount'] == '10000000.00'
        assert record['total_received_claims']['value'] == '179932000.00'
        assert record['total_admitted_claims']['value'] == '94801000.00'
        assert calculated['claim_received_difference_inr'] == '-1000.00'
        assert calculated['plan_payments_match_total'] is True
        assert calculated['asset_fair_difference_inr'] == '-0.50'
        assert calculated['plan_haircut_percent'] == '91.75'
        raw = next(item['source_rows'] for item in report['alternatives'] if 'source_rows' in item)
        assert any('GST' in row['creditor'] and row['plan_amount'] == '2180000.00' for row in raw)
        assert any('Liquidation was ordered' in warning for warning in report['warnings'])
    elif source['id'] == 'shitole':
        assert record['registered_office'] is None  # Applicant block, not debtor's office.
        assert record['irp']['value'] == 'Rajas Shreeram Bodas'
        assert any(item.get('previous',{}).get('value') == 'Anagha Anasingaraju'
                   for item in report['alternatives'] if isinstance(item.get('previous'),dict))
        assert record['liquidator']['value'] == 'Anagha Anasingaraju'
        assert record['outcome'] == 'liquidation_ordered'
        assert record['applicant'] is None and not record['plans'] and not record['plan_payments']
        assert record['total_admitted_claims']['value'] == '84152298.80'
        assert calculated['asset_fair_breakdown_inr'] == '282428.00'
        assert calculated['asset_liquidation_breakdown_inr'] == '217080.50'
        assert calculated['plan_haircut_percent'] is None
    elif source['id'] == 'gstaad':
        assert record['admission_date']['value'] == '2025-07-08'  # Set-aside first admission excluded.
        assert record['resolution_date']['value'] == '2026-09-03'
        assert record['coc_voting_share']['value'] == '98.96%'
        assert report['field_review']['coc_voting_share']['sources'][0]['page'] == 20
        assert record['plans'][0]['amount'] == '16110000000.00'
        assert record['plans'][0]['admitted'] == '11757619859.00'
        assert calculated['claim_received_matches_total'] is True
        assert calculated['claim_admitted_matches_total'] is True
        assert calculated['plan_payments_difference_inr'] == '-1689534.00'
        assert calculated['plan_haircut_percent'] == '-37.00'
        assert next(row for row in record['claims'] if row['category']=='employee')['admitted'] == '27026.00'
        assert len(record['financial_creditors']) == 4
        assert {r['value'] for r in record['previous_directors']} == {'Deepak B. Raheja','Anita D. Raheja','Sunil Ahuja'}
        assert any('Conflicting CoC' in warning for warning in report['warnings'])
    elif source['id'] == 'hyretail':
        assert record['professional']['value'] == record['irp']['value'] == 'Arunava Sikdar'
        assert record['liquidator']['value'] == 'Surinder Singh Panwar'
        assert record['liquidator']['page'] == 11
        assert [r['value'] for r in record['judges']] == ['Khetrabasi Biswal','Shishir Agarwal']
        assert all(r['page'] == 1 for r in record['judges'])
        assert {r['value'] for r in record['financial_creditors']} == {'AU Small Finance Bank Limited','Tata Capital Limited'}
        claims = {r['creditor']:r for r in record['claims']}
        assert len(claims) == 4
        assert claims['AU Small Finance Bank Limited']['claimed'] == '4373518.00'
        assert claims['AU Small Finance Bank Limited']['admitted'] == '4249246.00'
        assert claims['Tata Capital Limited']['claimed'] == '3792100.00'
        assert claims['Tata Capital Limited']['admitted'] == '3752738.00'
        assert claims['AU Small Finance Bank Limited']['page'] == claims['Tata Capital Limited']['page'] == 4
        assert claims['Employees’ State Insurance Corporation, Faridabad']['category'] == 'government'
        assert claims['Kiranakart Technologies Private Limited']['category'] == 'operational'
        assert claims['Kiranakart Technologies Private Limited']['admitted'] is None
        assert record['total_received_claims']['value'] == '8165618.00'
        assert record['total_admitted_claims']['value'] == '8001984.00'
        assert [m['percent'] for m in report['coc_members']] == ['53.10','46.90']
        assert record['outcome'] == 'liquidation_ordered'
        assert record['admission_date']['value'] == '2025-05-09'
        assert record['order_date']['value'] == '2026-09-10'
        assert record['registered_office'] is None  # RP residence and vehicle yard are not the registered office.
        assert not record['plans'] and not record['plan_payments']
        assert calculated['plan_haircut_percent'] is None
        assert report['field_review']['applicant']['status'] == 'not_applicable'
        assert any('creditor subset' in w for w in report['warnings'])
    else:
        assert record['plans'][0]['amount'] == '1640000000.00'  # Not creditor-only 163.29 crore.
        assert record['total_admitted_claims']['value'] == '10157044522.00'
        assert calculated['claim_received_matches_total'] is True
        assert calculated['claim_admitted_matches_total'] is True
        assert calculated['plan_payments_match_total'] is True
        assert calculated['asset_fair_difference_inr'] == '100000.00'
        assert next(row for row in record['claims'] if row['category']=='government')['claimed'] == '16574705.00'
        assert next(row for row in record['claims'] if row['category']=='employee')['admitted'] == '3667081.00'
        assert record['proposed_owners'][0]['value'] == 'Ambel Logistics Private Limited'
        assert record['proposed_owners'][0]['cin'] == 'U52100MH2024PTC424695'
        assert record['current_owners'][0]['value'] == 'Arshiya Limited'
        assert record['applicant_incorporated']['value'] == '2006-06-19'
        assert record['applicant_location']['value'] == 'Mauritius'
        assert record['industry']['value'].startswith('Free Trade and Warehousing Zone')


@pytest.mark.parametrize('source', sources(), ids=lambda source: source['id'])
def test_upload_populates_and_persists_draft_without_reopening_or_backfill(tmp_path, monkeypatch, source):
    # Real reader output is replayed at the reader boundary. Upload, extraction,
    # evidence validation, persistence, structured projection and API are real.
    monkeypatch.setattr('backend.main.extract_pdf',lambda content:copy.deepcopy(source['extraction']))
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/documents',files={'file':('unseen-name.pdf',b'%PDF-'+source['id'].encode(),'application/pdf')})
        assert response.status_code == 200, response.text
        detail = client.get('/api/cases/'+response.json()['case_id']).json()
        expected,_ = extract_case(source['extraction'],response.json()['document_id'])
        assert detail['record'] == expected
        assert detail['version'] == 1 and detail['review_status'] == 'draft'
        assert not detail['reviewed_by']
        assert [item['action'] for item in detail['audit']] == ['document_added']
        assert detail['documents'][0]['extraction']['case_facts']['version'] == VERSION
        assert client.get('/api/cases/'+response.json()['case_id']).json()['version'] == 1


def test_mixed_unknown_components_and_numeric_wrap_are_not_zero_or_concatenated():
    assert table_money('N/A\n27,026',money) == '27026.00'
    assert table_money('11,23,59,6\n0,063',money) == '11235960063.00'
    assert table_money('N/A',money) is None
    assert table_money('NIL',money) == '0.00'


def test_voting_conflict_rules_are_independent_of_company_names():
    source = next(item for item in sources() if item['id']=='gstaad')['extraction']
    encoded = json.dumps(source).replace('Gstaad Hotels','Future Hospitality').replace('99.93%','99.75%')
    source = json.loads(encoded)
    record,report = extract_case(source,'different-document')
    assert record['company']['value'] == 'Future Hospitality Private Limited'
    assert record['coc_voting_share']['value'] == '98.96%'
    assert any(item['field']=='coc_voting_share' and item['previous']['value']=='99.75%'
               for item in report['alternatives'])
