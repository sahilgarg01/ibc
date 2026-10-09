from datetime import date
from decimal import Decimal

from backend.research_analytics import case_profiles, research_analytics, select_recent_cases


def case(case_id, when, haircut, *, claim_scope='individual'):
    fact=lambda value: {'value':value,'document_id':'doc','page':1}
    admitted=100
    planned=admitted-haircut
    return {'id':case_id,'review_status':'approved',
            'record':{'company':fact('Company '+case_id),'case_number':fact('CP '+case_id),
                'resolution_date':fact(when) if when else None,'order_date':None,
                'outcome':'resolution_approved','irp':fact('Same IRP'),
                'professional':fact('Same RP'),
                'claims':[{'creditor':'Bank '+case_id,'category':'secured_financial',
                    'scope':claim_scope,'claimed':'120','admitted':'100',
                    'plan_amount':str(planned),'actual_paid':None}],
                'plans':[{'applicant':'Buyer','amount':str(planned),'admitted':'100'}],
                'previous_owners':[dict(fact('Owner'),cin='U12345MH2020PTC123456')],
                'subsequent_owners':[dict(fact('Owner New'),cin='U12345MH2020PTC123456')],
                'previous_directors':[],'subsequent_directors':[],
                'tribunal':fact('NCLT Mumbai'),'admission_date':fact('2019-01-01')},
            'metrics':{'plan_haircut_percent':str(haircut),
                'matched_admitted_inr':'100','matched_plan_inr':str(planned)}}


def test_ten_year_professional_pattern_and_claim_coverage():
    cases=[case('a','2020-01-01',60),case('b','2022-01-01',80,claim_scope='category_total'),
           case('old','2015-01-01',90),case('undated',None,70)]
    selected,period=select_recent_cases(cases,10,as_of=date(2026,9,30))
    assert [c['id'] for c in selected]==['a','b']
    assert period['from_date']=='2016-09-30'
    assert period['reviewed_without_date']==1 and period['reviewed_outside_period']==1
    result=research_analytics(selected)
    assert result['coverage']['resolution_cases']==2
    assert result['coverage']['with_party_claims']==1
    assert result['coverage']['with_identifier_confirmed_overlap']==2
    assert result['party_claims_by_type'][0]['party_rows']==1
    assert result['party_claims_by_type'][0]['filed_inr']=='120'
    assert len(result['professional_haircuts'])==2  # IRP and RP stay separate.
    irp=next(r for r in result['professional_haircuts'] if r['role']=='irp')
    assert irp['cases']==2 and Decimal(irp['average_haircut_percent'])==70
    assert Decimal(irp['weighted_haircut_percent'])==70
    assert irp['high_haircut_cases']==2 and irp['repeated_high_haircuts']
    assert {item['case_id'] for item in irp['case_details']}=={'a','b'}
    assert not research_analytics(selected[:1])['professional_haircuts'][0]['repeated_high_haircuts']


def test_profiles_include_approved_undated_cases_without_haircuts():
    first=case('a','2020-01-01',60)
    second=case('b',None,80)
    second['record']['outcome']='ongoing'
    second['record']['subsequent_owners']=[]
    second['metrics']['plan_haircut_percent']=None
    draft=case('draft','2022-01-01',50)
    draft['review_status']='draft'
    profiles=case_profiles([first,second,draft])
    irp=profiles['irp_profiles'][0]
    assert irp['cases']==2 and irp['approved_resolutions']==1
    assert irp['haircut_cases']==1 and irp['average_haircut_percent']=='60.00'
    assert {item['case_id'] for item in irp['case_details']}=={'a','b'}
    ownership={item['case_id']:item for item in profiles['ownership_comparisons']}
    assert ownership['a']['roles'][0]['confirmed'][0]['before']=='Owner'
    assert ownership['b']['roles'][0]['after']==[]
