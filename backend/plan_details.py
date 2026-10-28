"""Explicit rejected claims and proposed allocations that are not actual payments."""
import re
from decimal import Decimal


def enrich_rejected_claims(record, extraction, document_id, rows, evidence, money):
    nil_payment = any(re.search(r'Unsecured Financial Creditors\s+(?:Note:\s*Claims rejected in entirety\s+)?Nil\s+Nil\b',
        ' '.join(p['text'].split()), re.I) for p in extraction['pages'])
    for page in extraction['pages']:
        text = ' '.join(page['text'].split())
        if not re.search(r'total admitted claims? of the Unsecured Financial Creditors of the Corporate Debtor (?:is|are) NIL', text, re.I):
            continue
        layout = page.get('ocr_layout_text', '')
        heading = re.search(r'Unsecured Financial Claims received and admitted', layout, re.I)
        if not heading:
            continue
        block = layout[heading.end():]
        compact = re.sub(r'\s+', '', block[:700]).lower()
        if not all(word in compact for word in ('submitted', 'admitted', 'rejected')):
            continue
        candidates = []
        total = None
        for line in block.splitlines():
            amount = r'(\d[\d,]*\.\d{2})'
            match = re.fullmatch(r'\s*([A-Z][A-Za-z .&-]{2,100}?)\s{2,}' + amount + r'\s{2,}' + amount + r'\s*', line)
            if match and match[1].strip().lower() != 'total':
                if money(match[2]) != money(match[3]):
                    candidates = []
                    break
                candidates.append({'creditor':match[1].strip(), 'category':'unsecured_financial',
                    'scope':'individual', 'claimed':money(match[2]), 'admitted':'0.00',
                    'plan_amount':'0.00' if nil_payment else None,
                    'document_id':document_id, 'page':page['page'],
                    'notes':'Claims rejected in entirety; submitted and rejected amounts agree. Actual payment is not stated.'})
            match = re.fullmatch(r'\s*Total\s+' + amount + r'(?:\s+' + amount + r')?\s*', line, re.I)
            if match:
                total = money(match[1])
                break
        if not candidates or total is None or sum(Decimal(c['claimed']) for c in candidates) != Decimal(total):
            continue
        for row in list(rows):
            if row['category'] == 'unsecured_financial' and row.get('scope') == 'category_total' and row.get('admitted') in {None, '0.00', '0'} and (row.get('claimed') is None or Decimal(row['claimed']) == Decimal(total)):
                rows.remove(row)
        for row in candidates:
            if not any(r['category'] == row['category'] and r['creditor'].casefold() == row['creditor'].casefold() for r in rows):
                rows.append(row)
            if not any(f['value'].casefold() == row['creditor'].casefold() for f in record['financial_creditors']):
                record['financial_creditors'].append({'value':row['creditor'], 'document_id':document_id, 'page':page['page']})
        evidence.append({'field':'claims', 'page':page['page'], 'excerpt':block[:1000]})


def enrich_plan_allocations(record, extraction, document_id, evidence, money, warnings):
    if not record['applicant']:
        return
    number = r'(\d[\d,]*(?:\s+[\d,]+)*(?:\.\d+)?(?:\s*(?:crores?|lakhs?|lacs?|millions?))?)'
    def key(name):
        value = re.sub(r'[^a-z]', '', name.lower())
        if value in {'cirpcosts', 'corporateinsolvencyresolutionprocesscosts'}:
            return 'cirpcosts'
        if value in {'operationalcreditors', 'operationalcreditorscombined'}:
            return 'operationalcreditors'
        return value
    def add(recipient, amount, page, excerpt):
        if amount is None or any(key(p['recipient']) == key(recipient) for p in record['plan_payments']):
            return
        record['plan_payments'].append({'recipient':recipient, 'amount':amount,
            'admitted':None, 'document_id':document_id, 'page':page})
        evidence.append({'field':'plan_payments', 'page':page, 'excerpt':excerpt[:1000]})
    operational = None
    esi = None
    remaining = None
    for page in extraction['pages']:
        flat = ' '.join(page['text'].split())
        cost = re.search(r'\bSRA\b.{0,50}?proposes\s+to\s+pay\s+(?:Rs\.?|INR)\s*' + number + r'.{0,100}?towards\s+CIRP\s+costs', flat, re.I)
        if cost:
            add('CIRP costs', money(cost[1]), page['page'], cost.group())
            if re.search(r'estimated\s+CIRP\s+costs', flat, re.I):
                warnings.append('The CIRP cost allocation is estimated in the plan and payable at actuals; it is not an actual payment.')
        combined = re.search(r'provides\s+for\s+an?\s+amount\s+of\s+(?:Rs\.?|INR)\s*' + number + r'.{0,120}?to\s+be\s+paid\s+to\s+(?:the\s+)?Operational\s+Creditors', flat, re.I)
        if combined:
            operational = money(combined[1]), page['page'], combined.group()
        employee = re.search(r'Rs\.?\s*' + number + r'/\s*-\s*\([^)]{0,120}\)\s+shall\s+be\s+paid.{0,180}?Employees[\'’]?\s+State\s+Insurance\s+Corporation', flat, re.I)
        if employee:
            esi = money(employee[1]), page['page'], employee.group()
        others = re.search(r'remaining\s+amount\s+of\s+Rs\.?\s*' + number + r'.{0,180}?shall\s+be\s+apportioned\s+among\s+the\s+remaining\s+Operational\s+Creditors', flat, re.I)
        if others:
            remaining = money(others[1]), page['page'], others.group()
    if operational and operational[0] is not None:
        if esi and remaining and esi[0] is not None and remaining[0] is not None and Decimal(esi[0])+Decimal(remaining[0]) == Decimal(operational[0]):
            record['plan_payments'] = [p for p in record['plan_payments'] if key(p['recipient']) != 'operationalcreditors']
            add('Employees State Insurance Corporation', *esi)
            add('Other operational creditors (combined)', *remaining)
        else:
            add('Operational creditors (combined)', *operational)


def enrich_merged_operational_table(record, extraction, document_id, rows, evidence, money):
    """Keep a shared operational payment separate from its admitted subcategories."""
    token = r'(\d[\d,.]*\.\d{2})'
    def amount(raw):
        whole, cents = raw.rsplit('.', 1)
        return money(whole.replace('.', ',') + '.' + cents)
    for page in extraction['pages']:
        layout = page.get('ocr_layout_text', '')
        if not re.search(r'Total\s+claims\s+[\s\S]{0,120}?admitted', layout, re.I) or not re.search(r'Category\s+of\s*claimants', layout, re.I):
            continue
        government = re.search(r'^\s*-\s*Government\s*Dues\s{2,}' + token + r'\s*$', layout, re.I | re.M)
        employee = re.search(r'^\s*-\s*Workmen\s*/\s*Employees\s{2,}Nil\s{2,}' + token + r'\s{2,}\d+(?:\.\d+)?%\s*$', layout, re.I | re.M)
        other = re.search(r'^\s*-\s*Other\s*Operational\s+Creditors\s{2,}' + token + r'\s*$', layout, re.I | re.M)
        if not (government and employee and other and government.start() < employee.start() < other.start()):
            continue
        for category, name, value, match in (
                ('government', 'Government Dues', amount(government[1]), government),
                ('employee', 'Workmen/Employees', '0.00', employee),
                ('operational', 'Other Operational Creditors', amount(other[1]), other)):
            if value is None or any(r['category'] == category for r in rows):
                continue
            rows.append({'creditor':name, 'category':category, 'scope':'category_total',
                'claimed':None, 'admitted':value, 'plan_amount':None,
                'document_id':document_id, 'page':page['page'],
                'notes':'Admitted subcategory amount. The operational payment cell is shared across subcategories and is not allocated to this row.'})
            evidence.append({'field':'claims', 'page':page['page'], 'excerpt':match.group()})
