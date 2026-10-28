"""Resolve draft suggestions using case-specific roles and financial evidence.

These rules operate on PDF content, never filenames, debtor names or saved
transcriptions. Alternatives stay in the extraction report; only one mutually
exclusive claims breakdown enters the draft and its calculated summaries.
"""
import re
from collections import defaultdict
from decimal import Decimal

from .amount_units import table_unit, UNIT_FACTORS
from .field_rules import clean_entity, column_role
from .section_facts import table_money


def compact(value):
    return ' '.join((value or '').split())


def key(value):
    return re.sub(r'[^a-z]', '', value.casefold())


def financial_category(value):
    value = key(value)
    if 'unsecuredfinancialcreditor' in value:
        return 'unsecured_financial'
    if 'securedfinancialcreditor' in value:
        return 'secured_financial'
    if 'government' in value or 'gst' in value:
        return 'government'
    if 'employee' in value or 'workmen' in value:
        return 'employee'
    if 'operationalcreditor' in value:
        return 'operational'
    if 'otherdebts' in value:
        return 'other'
    return None


def _component_amounts(cell, money, unit):
    """Remove explicitly included PF subrows before adding other components."""
    result = []
    for line in (cell or '').splitlines():
        if re.search(r'already\s+included|\s+-\s*$', line, re.I):
            continue
        value = money(line.strip(), UNIT_FACTORS[unit])
        if value is not None:
            result.append(value)
    return result


def form_h_claims(extraction, document_id, money):
    """Read a continued stakeholder table, including merged operational rows."""
    rows, totals, evidence, completed = [], None, [], []
    columns, name_index, unit, context, last_page = None, 0, 'INR', None, None
    for page in extraction['pages']:
        number = page['page']
        if last_page is not None and number > last_page + 1:
            columns = None
        for table in page.get('tables', []):
            width = max((len(row) for row in table), default=0)
            if width < 4:
                continue
            header = [' '.join(compact(row[i]) for row in table[:2] if i < len(row))
                      for i in range(width)]
            roles = {}
            for i, cell in enumerate(header):
                role = column_role(cell)
                if role:
                    roles.setdefault(role, i)
            stakeholder = next((i for i, cell in enumerate(header)
                                if 'stakeholder' in key(cell)), None)
            if stakeholder is not None and all(role in roles for role in ('claimed', 'admitted', 'plan_amount')):
                if totals is not None:
                    rows, totals, evidence = [], None, []
                columns = [roles[role] for role in ('claimed', 'admitted', 'plan_amount')]
                name_index, context = stakeholder, None
                previous = next((p for p in extraction['pages'] if p['page'] == number - 1), {})
                unit = table_unit(page, table, previous)
            elif columns is None:
                continue
            elif any(re.search(r'fair\s+(?:market\s+)?value|nature\s+of\s+loss|name\s+of\s+creditor', h, re.I) for h in header):
                columns = None
                continue
            for cells in table:
                if max(columns + [name_index]) >= len(cells):
                    continue
                label = compact(cells[name_index])
                normalized = key(label)
                if not label or 'shareholder' in normalized:
                    continue
                amounts = [table_money(cells[i], money, unit) for i in columns]
                excerpt = ' | '.join(compact(cell) for cell in cells)
                if normalized in {'total', 'grandtotal'}:
                    totals = {'claimed': amounts[0], 'admitted': amounts[1],
                              'plan_amount': amounts[2], 'page': number, 'excerpt': excerpt}
                    completed.append((list(rows), totals, list(evidence)))
                    columns = None
                    break
                kind = financial_category(label)
                if all(amount is None for amount in amounts):
                    if kind in {'secured_financial', 'unsecured_financial', 'operational'}:
                        context = kind
                    continue
                # One grid cell can contain government, workmen, employee PF,
                # employee other dues and operational creditors. Only split when
                # every monetary column has the same four unambiguous components.
                merged = all(word in normalized for word in ('government', 'workmen', 'employee', 'otheroperational'))
                if merged:
                    components = [_component_amounts(cells[i], money, unit) for i in columns]
                    if all(len(values) == 4 for values in components):
                        for j, (name, cat) in enumerate((('Government dues', 'government'),
                                ('Workmen dues', 'employee'), ('Employee other dues', 'employee'),
                                ('Other operational creditors', 'operational'))):
                            rows.append({'creditor': name, 'category': cat, 'scope': 'category_total',
                                **dict(zip(('claimed', 'admitted', 'plan_amount'), (v[j] for v in components))),
                                'actual_paid': None, 'document_id': document_id, 'page': number,
                                'notes': 'Form H component; employee PF explicitly included in government is not counted again.'})
                            evidence.append({'field': 'claims', 'page': number, 'excerpt': excerpt[:1000]})
                    continue
                cat = kind or context
                if not cat:
                    continue
                # A parent amount supersedes its assenting/dissenting subrows.
                subgroup = bool(re.match(r'^-?\s*(?:\(?[ivx]+\)?\s*)?(?:assenting|dissenting|creditors\s+not\s+having)', label, re.I))
                parent = re.fullmatch(r'(?:unsecured|secured)financialcreditors?', normalized)
                if subgroup and any(row['category'] == cat and row.get('_parent') for row in rows):
                    evidence.append({'field': 'claims', 'page': number, 'excerpt': excerpt[:1000]})
                    continue
                rows.append({'creditor': (cat.replace('_', ' ').title() + ' creditors — ' + label) if subgroup else label,
                    'category': cat, 'scope': 'category_total', 'claimed': amounts[0],
                    'admitted': amounts[1], 'plan_amount': amounts[2], 'actual_paid': None,
                    'document_id': document_id, 'page': number,
                    'notes': 'Form H proposed allocation, not actual payment. Unknown components remain undocumented.',
                    '_parent': bool(parent)})
                evidence.append({'field': 'claims', 'page': number, 'excerpt': excerpt[:1000]})
                if cat in {'secured_financial', 'unsecured_financial'}:
                    context = cat
            last_page = number
    # Without a printed total or at least two quantified categories this is not
    # a safely recognized complete stakeholder schedule. Retain other readers.
    complete = [item for item in completed if len({row['category'] for row in item[0]}) >= 2]
    if not complete:
        return None
    rows, totals, evidence = complete[-1]
    groups = defaultdict(list)
    for row in rows:
        # A late claim with only a proposed allocation cannot be added to a
        # known admitted category total. Retain it in raw evidence/payments;
        # do not erase the quantified government's admitted amount.
        if row['claimed'] is None and row['admitted'] is None:
            continue
        groups[row['category']].append(row)
    consolidated = []
    for cat, items in groups.items():
        # No fabricated zero for a category with an unknown constituent amount.
        merged = {k: v for k, v in items[0].items() if not k.startswith('_')}
        if len(items) > 1:
            merged['creditor'] = cat.replace('_', ' ').title() + ' creditors (combined subcategories)'
            merged['notes'] = ('Disjoint Form H subcategories combined without counting parent totals twice. ' +
                '; '.join(f"p{r['page']} {r['creditor']}: claimed {r['claimed']}, admitted {r['admitted']}, proposed {r['plan_amount']}" for r in items))[:2000]
            for field in ('claimed', 'admitted', 'plan_amount'):
                values = [row[field] for row in items]
                merged[field] = str(sum((Decimal(v) for v in values), Decimal(0))) if all(v is not None for v in values) else None
        consolidated.append(merged)
    return consolidated, rows, totals, evidence


def enrich_evidence_rules(record, extraction, document_id, evidence, money, iso_date, warnings):
    """Apply stronger explicit evidence and return all displaced alternatives."""
    alternatives = []
    reported_totals = []
    def set_fact(field, value, page, excerpt):
        if value is None:
            return
        old = record.get(field)
        if old and old['value'] != value:
            alternatives.append({'field': field, 'previous': old, 'selected': value,
                                 'page': page, 'reason': 'Explicit scoped evidence.'})
        record[field] = {'value': value, 'document_id': document_id, 'page': page}
        evidence.append({'field': field, 'page': page, 'excerpt': excerpt[:1000]})
    def add_name(field, value, page, excerpt, cin=None):
        value = clean_entity(value)
        existing = next((row for row in record[field] if key(row['value']) == key(value)),None)
        if existing and value.count(' ') > existing['value'].count(' '):
            # Prefer independently printed word boundaries only when every
            # letter agrees; do not invent OCR spelling repairs.
            existing['value'] = value
        if 2 <= len(value) <= 500 and not any(key(row['value']) == key(value) for row in record[field]):
            record[field].append({'value': value, 'document_id': document_id, 'page': page, 'cin': cin})
            evidence.append({'field': field, 'page': page, 'excerpt': excerpt[:1000]})

    form = form_h_claims(extraction, document_id, money)
    if form:
        chosen, raw, totals, sources = form
        reported_totals.append({**totals,'document_id':document_id,'label':'Form H stakeholder total'})
        alternatives.append({'field': 'claims', 'previous': record['claims'],
                             'source_rows': [{k:v for k,v in row.items() if not k.startswith('_')} for row in raw],
                             'reason': 'Use the explicit Form H stakeholder snapshot; retain earlier named schedules as evidence.'})
        # Unknown fields in a recognized category may have fuller evidence in
        # another schedule. Do not overwrite explicit actual-payment evidence.
        if not any(row.get('actual_paid') is not None for row in record['claims']):
            record['claims'] = chosen
        evidence.extend(sources)
        proposed = [row['plan_amount'] for row in chosen]
        if totals['plan_amount'] is not None and all(value is not None for value in proposed):
            # Include any late-claim allocation retained outside the admitted
            # breakdown when comparing the printed distribution total.
            proposed += [row['plan_amount'] for row in raw if row['claimed'] is None and row['admitted'] is None and row['plan_amount'] is not None]
            difference = sum((Decimal(v) for v in proposed),Decimal(0)) - Decimal(totals['plan_amount'])
            if difference:
                warnings.append(f'Form H allocation rows differ from the printed stakeholder payment total by INR {difference}; neither figure was silently corrected.')
        for field, source in [('total_received_claims', 'claimed'), ('total_admitted_claims', 'admitted')]:
            if totals[source] is not None:
                set_fact(field, totals[source], totals['page'], totals['excerpt'])
        if len(record['plans']) == 1 and totals['admitted'] and Decimal(totals['admitted']) > 0:
            record['plans'][0]['admitted'] = totals['admitted']
            evidence.append({'field': 'plans', 'page': totals['page'], 'excerpt': totals['excerpt'][:1000]})

    # Explicit outlay tables include CIRP costs. A Form H creditor distribution
    # total does not, by itself, establish the entire resolution consideration.
    for page in extraction['pages']:
        for table in page.get('tables', []):
            if not table or len(table[0]) != 3:
                continue
            header = [key(cell or '') for cell in table[0]]
            if not ('creditor' in header[1] and 'amountproposed' in header[2]):
                continue
            payments, total = [], None
            for row in table[1:]:
                if len(row) != 3:
                    continue
                name = compact(row[1]); value = compact(row[2])
                if key(name) == 'total':
                    total = table_money(value, money)
                    continue
                estimate = re.search(r'(?:at\s+actuals?\s*[–—-]\s*)([\d,]+(?:\.\d+)?)', value, re.I)
                amount = money(estimate[1]) if estimate else table_money(value, money)
                if amount is not None:
                    payments.append({'recipient': name[:200], 'amount': amount, 'admitted': None,
                                     'document_id': document_id, 'page': page['page']})
            if total is None or not payments or not record['applicant']:
                continue
            alternatives.append({'field': 'plan_payments', 'previous': record['plan_payments'],
                                 'reason': 'Explicit financial outlay including costs replaces creditor-only allocations.'})
            record['plan_payments'] = payments
            record['plans'] = [{'applicant': record['applicant']['value'], 'amount': total,
                'admitted': record['total_admitted_claims']['value'] if record['total_admitted_claims'] else None,
                'document_id': document_id, 'page': page['page']}]
            excerpt = ' | '.join(compact(cell) for row in table for cell in row)
            for field in ('plans', 'plan_payments'):
                evidence.append({'field': field, 'page': page['page'], 'excerpt': excerpt[:1000]})

    # Voting tables are more precise than introductory, rounded statements.
    voting = []
    for page in extraction['pages']:
        for table in page.get('tables', []):
            if not table:
                continue
            header = [key(cell or '') for cell in table[0]]
            ni = next((i for i,h in enumerate(header) if 'name' in h and ('creditor' in h or h in {'name','nameofthemembers'})), None)
            vi = next((i for i,h in enumerate(header) if 'votingshare' in h or 'votepower' in h or 'votingpercentage' in h), None)
            si = next((i for i,h in enumerate(header) if 'votingforresolutionplan' in h), None)
            if si is None:
                si = next((i for i,cell in enumerate(table[0]) if re.search(r'Agenda\s*-?\s*1\b',cell or '',re.I)),None)
            if ni is None or vi is None:
                continue
            members=[]
            for row in table[1:]:
                if len(row) <= max(ni,vi):
                    continue
                percent = re.fullmatch(r'(\d+(?:\.\d+)?)\s*%?', compact(row[vi]))
                if not percent:
                    continue
                name = compact(row[ni]); status = compact(row[si]) if si is not None and len(row)>si else ''
                if key(name) in {'total','grandtotal'}:
                    continue
                if name:
                    add_name('financial_creditors', name, page['page'], ' | '.join(compact(c) for c in row))
                    members.append({'name':name, 'percent':percent[1], 'status':status, 'page':page['page']})
            if members:
                voting = members
                approved = [Decimal(r['percent']) for r in members if re.fullmatch(r'Voted\s+For|Assented|Yes',r['status'],re.I)]
                if approved and abs(sum((Decimal(r['percent']) for r in members),Decimal(0))-100) <= Decimal('.02'):
                    value = format(sum(approved, Decimal(0)).normalize(),'f')+'%'
                    set_fact('coc_voting_share', value, page['page'], '; '.join(f"{m['name']} {m['percent']}% {m['status']}" for m in members))

    person = r'(?:Mr\.?|Ms\.?|Mrs\.?)\s+((?-i:(?:[A-Z][A-Za-z]+|[A-Z]\.)(?:\s+(?:[A-Z][A-Za-z]+|[A-Z]\.)){1,4}?))(?=[,.;]|\s+(?:is|was|and|who)\b)'
    for page in extraction['pages']:
        text = compact(page.get('layout_text') or page.get('embedded_text', page['text']))
        n = page['page']
        second_admission = re.search(r'order\s+dated\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})\s*\([“"\s]*Second\s+Admission\s+Order',text,re.I)
        if second_admission:
            value = iso_date(second_admission[1])
            if value:
                set_fact('admission_date', value, n, second_admission.group())
        if record['professional'] and re.search(r'\bSubsequently\b', text, re.I):
            professional = re.escape(record['professional']['value']).replace(r'\ ',r'\s+')
            replacement = re.search(r'\bSubsequently\b.{0,300}?appointed\s+(?:(?:CS\.?|CA\.?|Mr\.?|Ms\.?)\s*)?(?:\(Dr\.?\)\s*)?'+professional+r'\s+as\s+(?:the\s+)?Interim\s+Resolution\s+Professional',text,re.I)
            if replacement:
                set_fact('irp',record['professional']['value'],n,replacement.group())
        if not record['industry']:
            business = re.search(r'corporate\s+debtor\s+(?:was|is)\s+in\s+(?:the\s+)?provisions?\s+of\s+(.{5,100}?)\s+to\b', text, re.I)
            if business:
                set_fact('industry', business[1].strip(' “"'), n, business.group())
            project = re.search(r'\b(Free\s+Trade\s+and\s+Warehousing\s+Zone\s*\(FTWZ\)\s+project)\s+of\s+the\s+Corporate\s+Debtor',text,re.I)
            if project:
                set_fact('industry',project[1],n,project.group())
        # Named finance counterparties can appear only in narrative discussion.
        for m in re.finditer(r'\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+(?:Sahakari\s+)?Bank\s+Limited)\s+(?:holding|filed\s+its\s+claim)', text):
            add_name('financial_creditors', m[1], n, m.group())
        if not record['proposed_owners']:
            allotted = re.search(r'shall\s+issue\s+and\s+allot\b.{0,150}?\bto\s+([A-Z][A-Za-z0-9 &.-]{2,100}?(?:Private\s+Limited|Pvt\.?\s*Ltd\.?|Limited))', text, re.I)
            if allotted:
                name = allotted[1]
                cin = None
                for source in extraction['pages']:
                    name_pattern = re.escape(name).replace(r'\ ', r'\s+')
                    name_pattern = name_pattern.replace(r'Private\s+Limited',r'(?:Private\s+Limited|Pvt\.?\s*Ltd\.?)')
                    match = re.search(name_pattern + r'\s*\(CIN\s*[-:]\s*([LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\)',compact(source['text']),re.I)
                    if match:
                        cin = match[1]
                add_name('proposed_owners', name, n, allotted.group(), cin)
        holding = re.search(r'([A-Z][A-Za-z0-9]*(?:\s+(?:[A-Z][A-Za-z0-9]*|&)){0,7}\s+Limited)\s*\(Holding\s+Company\b', text)
        if holding:
            add_name('current_owners', holding[1], n, holding.group())
        for m in re.finditer(r'\bDirector\s+of\s+the\s+Corporate\s+Debtor\s+'+person, text, re.I):
            add_name('previous_directors', m[1], n, m.group())
        shareholding = re.search(r'([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+Limited)\s*\([^)]{1,10}\)\s*,?\s*which\s+held\s+approximately\s+\d+(?:\.\d+)?%\s+shareholding\s+(?:in\s+)?the\s+Corporate\s+Debtor', text)
        if shareholding:
            add_name('current_owners', shareholding[1], n, shareholding.group())
        next_page = next((p for p in extraction['pages'] if p['page'] == n + 1), {})
        continued = text + ' ' + compact(next_page.get('layout_text') or next_page.get('text',''))
        title = r'(?:Mr|Mrs|Ms)\.?\s+'
        named = r'((?-i:[A-Z][A-Za-z.]*(?:\s+[A-Z][A-Za-z.]*){1,4}?))'
        objectors = re.search(title + named + r'\s+and\s+' + title + named +
            r'\s*\(.{0,180}?\),\s*in\s+their\s+capacity\s+as\s+members\s+of\s+the\s+Suspended\s+Board\s+of\b.{0,300}?Directors\s+of\s+' +
            re.escape(record['company']['value']).replace(r'\ ',r'\s+') if record['company'] else r'(?!)', continued, re.I)
        if objectors:
            for value in objectors.group(1,2):
                add_name('previous_directors', value, n, objectors.group())
        bidders = re.search(r'Resolution\s+Plans\s+received\s+from\s+(.{5,500}?)(?=,\s*along\s+with)',text,re.I)
        if bidders:
            for value in re.split(r',\s*|\s+and\s+',bidders[1]):
                if re.search(r'(?:Limited|Ltd\.?)\s*$',value,re.I):
                    add_name('plan_submitters',value,n,bidders.group())

    # Repair a PDF-split role name only when its letters exactly match an
    # independently recovered name for the same professional.
    if record['irp'] and record['professional']:
        if key(record['irp']['value']) == key(record['professional']['value']):
            record['irp']['value'] = record['professional']['value']
    for row in record['asset_values']:
        for field, amount in (('fair_value_page','fair_value'),('liquidation_value_page','liquidation_value')):
            if row[amount] is not None and row.get(field) is None:
                row[field] = row['page']
    named_creditors, seen = [], set()
    for row in record['financial_creditors']:
        value = re.sub(r'\s*\([^)]*(?:replaced|formerly)[^)]*\)','',row['value'],flags=re.I).strip()
        normalized = key(value)
        if normalized in seen or normalized in {'limited','privatelimited','pvtltd'} or re.match(
                r'^(?:(?:unsecured|secured)financialcreditors?|creditorsnothaving|assenting|dissenting)',normalized):
            continue
        named_creditors.append({**row,'value':value});seen.add(normalized)
    record['financial_creditors'] = named_creditors
    if any(a['field'] == 'coc_voting_share' for a in alternatives):
        warnings.append('Conflicting CoC percentages: the structured voting result was selected; earlier narrative percentages remain available for review.')
    if form and any(row['claimed'] is None and row['admitted'] is None for row in form[1]):
        warnings.append('A Form H allocation has no quantified claimed/admitted amount. It remains in source rows and payment allocations, outside the matched claim calculation.')
    if alternatives:
        warnings.append('Alternative PDF figures or schedules were retained in the extraction report; review the selected evidence and reconciliation warnings.')
    return {'alternatives': alternatives, 'coc_members': voting, 'financial_table_totals':reported_totals}


def reconciliation_warnings(record, metrics):
    """Name real discrepancies; never silently repair a printed source total."""
    result=[]
    if (record['total_received_claims'] or record['total_admitted_claims']) and any(
            row['claimed'] is None or row['admitted'] is None for row in record['claims']):
        result.append('Some claim amounts are undocumented. Check whether the reported totals cover only a creditor subset; unknown claims are not zero.')
    for field,label in [('claim_received_matches_total','Claims received'),
                        ('claim_admitted_matches_total','Claims admitted'),
                        ('asset_fair_matches_total','Fair value'),
                        ('asset_liquidation_matches_total','Liquidation value'),
                        ('plan_payments_match_total','Payment allocations')]:
        if metrics[field] is False:
            result.append(label+' breakdown does not match the stated total; source values were retained.')
    if record['outcome']=='liquidation_ordered' and record['plans']:
        result.append('Liquidation was ordered. Extracted resolution-plan figures are proposals, not an approved resolution or actual recovery.')
    return result
