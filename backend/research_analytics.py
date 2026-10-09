from collections import defaultdict
from datetime import date
from decimal import Decimal
from .structured import normalized


def select_recent_cases(cases, years=10, as_of=None):
    """Use the final resolution date, then the order date; undated cases stay visible as gaps."""
    today = as_of or date.today()
    try:
        cutoff = today.replace(year=today.year-years) if years else None
    except ValueError:  # February 29 when the cutoff year is not a leap year.
        cutoff = today.replace(year=today.year-years, day=28)
    recent, undated, outside = [], 0, 0
    for case in cases:
        record = case['record']
        raw = (record.get('resolution_date') or record.get('order_date') or {}).get('value')
        try:
            day = date.fromisoformat(raw) if raw else None
        except ValueError:
            day = None
        if not day:
            undated += 1
        elif (cutoff is None or day >= cutoff) and day <= today:
            recent.append(case)
        else:
            outside += 1
    return recent, {'years':years,'from_date':cutoff.isoformat() if cutoff else None,
                    'through_date':today.isoformat(),'reviewed_in_period':len(recent),
                    'reviewed_without_date':undated,'reviewed_outside_period':outside}


def case_profiles(cases):
    """Evidence-linked ownership comparisons and IRP name groups for approved cases."""
    irps = defaultdict(list)
    ownership = []
    for case in cases:
        if case['review_status'] != 'approved':
            continue
        record = case['record']
        company = (record.get('company') or {}).get('value') or 'Company not recorded'
        order_date = (record.get('resolution_date') or record.get('order_date') or {}).get('value')
        irp = record.get('irp')
        if irp and irp.get('value'):
            key = normalized(irp['value'])
            if key:
                irps[key].append({'case_id':case['id'],'company':company,
                    'case_number':(record.get('case_number') or {}).get('value'),
                    'order_date':order_date,'outcome':record.get('outcome'),
                    'haircut_percent':case['metrics'].get('plan_haircut_percent'),
                    'evidence':irp})
        roles = []
        for role in ('owners','directors'):
            before = record.get('previous_'+role) or []
            current = record.get('current_'+role) or []
            after = record.get('subsequent_'+role) or []
            if not before and not current and not after:
                continue
            confirmed, name_candidates = [], []
            for old in before:
                for new in after:
                    old_ids = {kind:old.get(kind).strip().upper() for kind in ('din','cin') if old.get(kind)}
                    new_ids = {kind:new.get(kind).strip().upper() for kind in ('din','cin') if new.get(kind)}
                    if any(old_ids.get(kind) and old_ids.get(kind)==new_ids.get(kind) for kind in ('din','cin')):
                        if not any(old_ids.get(kind) and new_ids.get(kind) and old_ids[kind]!=new_ids[kind] for kind in ('din','cin')):
                            confirmed.append({'before':old['value'],'after':new['value']})
                    elif normalized(old['value']) and normalized(old['value'])==normalized(new['value']):
                        if not any(old_ids.get(kind) and new_ids.get(kind) and old_ids[kind]!=new_ids[kind] for kind in ('din','cin')):
                            name_candidates.append({'before':old['value'],'after':new['value']})
            roles.append({'role':role,'before':before,'current':current,'after':after,
                'confirmed':confirmed,'name_candidates':name_candidates})
        if roles:
            ownership.append({'case_id':case['id'],'company':company,
                'case_number':(record.get('case_number') or {}).get('value'),
                'order_date':order_date,'roles':roles})
    profiles = []
    for key, rows in sorted(irps.items()):
        haircuts = [Decimal(str(row['haircut_percent'])) for row in rows if row['haircut_percent'] is not None]
        profiles.append({'name':rows[0]['evidence']['value'],'name_key':key,
            'cases':len(rows),'approved_resolutions':sum(row['outcome']=='resolution_approved' for row in rows),
            'haircut_cases':len(haircuts),
            'average_haircut_percent':str(round(sum(haircuts)/len(haircuts),2)) if haircuts else None,
            'case_details':sorted(rows,key=lambda row:(row['order_date'] or '',row['case_id']),reverse=True)})
    return {'irp_profiles':profiles,'ownership_comparisons':sorted(ownership,
        key=lambda row:(row['order_date'] or '',row['case_id']),reverse=True)}


def research_analytics(cases):
    professionals, benches, overlaps = defaultdict(list), defaultdict(list), []
    claim_groups = defaultdict(lambda:{'rows':0,'filed_rows':0,'admitted_rows':0,
                                       'filed':Decimal(0),'admitted':Decimal(0),'case_ids':set()})
    professional_names = {}
    missing = 0
    coverage = {'resolution_cases':0,'with_party_claims':0,'with_admitted_claims':0,
                'with_plans':0,'with_haircut':0,'with_actual_payments':0,
                'with_before_after_people':0,
                'with_identifier_confirmed_overlap':0}
    for case in cases:
        r = case["record"]
        if case["review_status"] != "approved":
            continue
        if r["outcome"] == "resolution_approved":
            coverage['resolution_cases'] += 1
            if any(r.get('previous_'+role) and r.get('subsequent_'+role) for role in ('owners','directors')):
                coverage['with_before_after_people'] += 1
            if any(c['scope']=='individual' for c in r.get('claims',[])):
                coverage['with_party_claims'] += 1
            if any(c.get('admitted') is not None for c in r.get('claims',[])):
                coverage['with_admitted_claims'] += 1
            if any(c.get('actual_paid') is not None for c in r.get('claims',[])):
                coverage['with_actual_payments'] += 1
            for claim in r.get('claims',[]):
                if claim['scope']!='individual':
                    continue  # Category totals must not be counted as people/parties.
                group=claim_groups[claim['category']]
                group['rows']+=1
                group['case_ids'].add(case['id'])
                if claim.get('claimed') is not None:
                    group['filed_rows']+=1
                    group['filed']+=Decimal(claim['claimed'])
                if claim.get('admitted') is not None:
                    group['admitted_rows']+=1
                    group['admitted']+=Decimal(claim['admitted'])
            if r.get('plans'):
                coverage['with_plans'] += 1
            haircut = case["metrics"]["plan_haircut_percent"]
            admitted = Decimal(case['metrics']['matched_admitted_inr'])
            planned = Decimal(case['metrics']['matched_plan_inr'])
            basis = 'matched_claims'
            if haircut is None and len(r.get("plans", [])) == 1:
                p = r["plans"][0]
                if p["admitted"] and p["amount"] is not None:
                    haircut = (Decimal(p["admitted"])-Decimal(p["amount"]))/Decimal(p["admitted"])*100
                    admitted, planned, basis = Decimal(p['admitted']),Decimal(p['amount']),'plan_total'
            if haircut is not None:
                coverage['with_haircut'] += 1
                for role in ("irp", "professional"):
                    if r.get(role):
                        name=r[role]['value']
                        key=(role,' '.join(name.casefold().split()))
                        professional_names.setdefault(key,name)
                        professionals[key].append({'case_id':case['id'],
                            'company':(r.get('company') or {}).get('value'),
                            'case_number':(r.get('case_number') or {}).get('value'),
                            'haircut_percent':Decimal(haircut),'admitted':admitted,
                            'planned':planned,'basis':basis})
        if r.get("tribunal") and r.get("admission_date") and r.get("resolution_date"):
            days = (date.fromisoformat(r["resolution_date"]["value"])-date.fromisoformat(r["admission_date"]["value"])).days
            if days >= 0:
                benches[r["tribunal"]["value"]].append(days)
        else:
            missing += 1
        confirmed, candidates = [], []
        for role in ("owners", "directors"):
            for before in r.get("previous_" + role, []):
                for after in r.get("subsequent_" + role, []):
                    matched = any(before.get(k) and before.get(k) == after.get(k) for k in ("din", "cin"))
                    conflicts = any(before.get(k) and after.get(k) and before[k] != after[k] for k in ("din", "cin"))
                    item = {"role": role, "before": before["value"], "after": after["value"]}
                    if matched and not conflicts:
                        confirmed.append(item)
                    elif normalized(before["value"]) == normalized(after["value"]) and not conflicts:
                        candidates.append(item)
        overlaps.append({"case_id": case["id"], "company": (r.get("company") or {}).get("value"),
                         "created_at": case.get("created_at"),
                         "confirmed": confirmed, "name_candidates": candidates})
        if confirmed and r['outcome']=='resolution_approved':
            coverage['with_identifier_confirmed_overlap'] += 1
    professional_rows=[]
    for key,case_rows in sorted(professionals.items()):
        admitted=sum((item['admitted'] for item in case_rows),Decimal(0))
        planned=sum((item['planned'] for item in case_rows),Decimal(0))
        professional_rows.append({'role':key[0],'professional':professional_names[key],
            'cases':len(case_rows),
            'average_haircut_percent':str(round(sum(item['haircut_percent'] for item in case_rows)/len(case_rows),2)),
            'weighted_haircut_percent':str(round((admitted-planned)/admitted*100,2)) if admitted else None,
            'high_haircut_cases':sum(item['haircut_percent']>=50 for item in case_rows),
            'repeated_high_haircuts':len(case_rows)>=2 and all(item['haircut_percent']>=50 for item in case_rows),
            'case_details':[{'case_id':item['case_id'],'company':item['company'],
                'case_number':item['case_number'],'haircut_percent':str(round(item['haircut_percent'],2)),
                'basis':item['basis']} for item in case_rows]})
    return {
        "professional_haircuts": professional_rows,
        'party_claims_by_type':[{'category':category,'party_rows':group['rows'],
            'filed_rows':group['filed_rows'],'admitted_rows':group['admitted_rows'],
            'filed_inr':str(group['filed']),'admitted_inr':str(group['admitted']),
            'case_ids':sorted(group['case_ids'])} for category,group in sorted(claim_groups.items())],
        "bench_timelines": [{"bench": name, "cases": len(v), "average_days": round(sum(v)/len(v), 1),
            "min_days": min(v), "max_days": max(v)} for name, v in sorted(benches.items())],
        "missing_timeline_cases": missing, "ownership_overlap": overlaps,
        "coverage":coverage}
