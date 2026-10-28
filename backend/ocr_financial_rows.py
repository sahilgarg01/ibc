"""Conservative fallback for complete category rows in OCR table layouts."""
import re
from decimal import Decimal


def enrich_ocr_claim_summary(record, extraction, document_id, rows, evidence, warnings):
    """Read a labelled claims abstract independently of principal/interest tables."""
    scales = {'crores':Decimal('10000000'), 'lakhs':Decimal('100000')}
    for page in extraction['pages']:
        layout = page.get('ocr_layout_text', '')
        heading = re.search(r'abstract\s+of\s+claims\s+received\s*[.:]*', layout, re.I)
        if not heading:
            continue
        block = layout[heading.end():]
        unit = re.search(r'Rupees\s+in\s+(crores|lakhs)', block[:250], re.I)
        if not unit:
            continue
        compact = re.sub(r'\s+', '', block[:700]).lower()
        if not ('claimed' in compact and 'admitted' in compact and compact.index('claimed') < compact.index('admitted')):
            continue
        scale = scales[unit[1].lower()]
        lines = block.splitlines()
        candidates = []
        total = None
        for i, line in enumerate(lines):
            match = re.search(r'(\d+\.\d{2}(?:\s+\d+\.\d{2}){1,2})\s*$', line)
            if not match:
                continue
            amounts = [str((Decimal(v)*scale).quantize(Decimal('.01'))) for v in match[1].split()]
            prefix = line[:match.start()]
            if re.search(r'\bTotal\b', prefix, re.I):
                total = amounts[:2], line
                break
            context = ' '.join(lines[max(0, i-1):i+1])
            label = re.search(r'(Unsecured\s+Financial|Secured\s+Financial|Operational)\s*(?:Creditors)?', context, re.I)
            if not label:
                continue
            category = {'unsecured financial':'unsecured_financial',
                'secured financial':'secured_financial', 'operational':'operational'}[' '.join(label[1].lower().split())]
            candidates.append({'creditor':' '.join(label[1].split()) + ' Creditors',
                'category':category, 'scope':'category_total', 'claimed':amounts[0], 'admitted':amounts[1],
                'plan_amount':None, 'document_id':document_id, 'page':page['page'],
                'notes':'Claim abstract in ' + unit[1].lower() + '; under-verification amounts are not plan payments.'})
        if not total or not candidates:
            continue
        # A mismatching source total remains visible, rather than silently
        # altering a printed amount to make the arithmetic agree.
        for index, field in enumerate(('total_received_claims', 'total_admitted_claims')):
            if not record[field]:
                record[field]={'value':total[0][index], 'document_id':document_id, 'page':page['page']}
                evidence.append({'field':field, 'page':page['page'], 'excerpt':total[1]})
            amount_field = ('claimed', 'admitted')[index]
            if sum(Decimal(r[amount_field]) for r in candidates) != Decimal(total[0][index]):
                warnings.append('The printed claims abstract total differs from its category rows; verify the source reconciliation.')
        for row in candidates:
            if not any(r['category'] == row['category'] for r in rows):
                rows.append(row)
                evidence.append({'field':'claims', 'page':page['page'], 'excerpt':block[:1000]})
        # A detailed SFC schedule follows some abstracts. Its principal and
        # interest columns must not be mistaken for claimed/admitted values.
        detail_heading = re.search(r'Secured\s*Financial\s*Creditors\s*:', block, re.I)
        if not detail_heading:
            continue
        detail_block = block[detail_heading.end():]
        if not re.search(r'Amount\s+in\s*rupees\s+(?:in\s+)?' + unit[1], detail_block[:150], re.I):
            continue
        detail_lines = detail_block.splitlines()
        details = []
        for i, line in enumerate(detail_lines):
            if re.search(r'Operational\s+Creditors\s*:', line, re.I):
                break
            match = re.search(r'\bSFC\s+\d{2}[./]\d{2}[./]\d{2,4}\s+(\d+\.\d{2})\s+(\d+\.\d{2})\s+(\d+\.\d{2})\s+(\d+\.\d{2})\s*$', line)
            if not match:
                continue
            context = ' '.join(detail_lines[max(0,i-1):i+2]).lower()
            parties = []
            for party in record['financial_creditors']:
                tokens = [t for t in re.findall(r'[a-z]+', party['value'].lower()) if t not in {'bank', 'limited', 'ltd'}]
                if tokens and all(t in context for t in tokens):
                    parties.append(party['value'])
            if len(parties) != 1:
                continue
            details.append({'creditor':parties[0], 'category':'secured_financial', 'scope':'individual',
                'claimed':str((Decimal(match[3])*scale).quantize(Decimal('.01'))),
                'admitted':str((Decimal(match[4])*scale).quantize(Decimal('.01'))),
                'plan_amount':None, 'document_id':document_id, 'page':page['page'],
                'notes':'Detailed secured creditor schedule in ' + unit[1] + '; total claim is separate from principal and interest.'})
        aggregates = [r for r in rows if r['category'] == 'secured_financial' and r.get('scope') == 'category_total']
        if len(aggregates) == 1 and len(details) > 1 and len({r['creditor'] for r in details}) == len(details) and all(
                sum(Decimal(r[k]) for r in details) == Decimal(aggregates[0][k]) for k in ('claimed', 'admitted')):
            rows.remove(aggregates[0])
            rows.extend(details)
            evidence.append({'field':'claims', 'page':page['page'], 'excerpt':detail_block[:1000]})
        op_heading = re.search(r'Operational\s+Creditors\s*:', detail_block, re.I)
        if op_heading:
            op_details = []
            narrative = re.search(r'Financial\s+Creditors\s*,?\s+and\s*(.{3,100}?)\s+as\s+Operational\s+Creditors', ' '.join(page['text'].split()), re.I)
            for line in detail_block[op_heading.end():].splitlines():
                match = re.search(r'\bOC\s+(.{3,80}?)\s+\d{2}[./]\d{2}[./]\d{2,4}\s+((?:\d+\.\d{2}\s+){2,}\d+\.\d{2})\s*$', line)
                if not match:
                    continue
                values = match[2].split()
                name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', match[1]).strip()
                if narrative:
                    full_name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', narrative[1]).strip()
                    if re.sub(r'\W', '', full_name).lower().startswith(re.sub(r'\W', '', name).lower()):
                        name = full_name
                op_details.append({'creditor':name,
                    'category':'government' if re.search(r'\bTax\b', name, re.I) else 'operational',
                    'scope':'individual', 'claimed':str((Decimal(values[-3])*scale).quantize(Decimal('.01'))),
                    'admitted':str((Decimal(values[-2])*scale).quantize(Decimal('.01'))),
                    'plan_amount':None, 'document_id':document_id, 'page':page['page'],
                    'notes':'Detailed operational creditor schedule; under-verification amount is not a plan payment.'})
            aggregates = [r for r in rows if r['category'] == 'operational' and r.get('scope') == 'category_total']
            if len(aggregates) == 1 and op_details and all(
                    sum(Decimal(r[k]) for r in op_details) == Decimal(aggregates[0][k]) for k in ('claimed', 'admitted')):
                rows.remove(aggregates[0])
                rows.extend(op_details)
                evidence.append({'field':'claims', 'page':page['page'], 'excerpt':detail_block[op_heading.start():][:1000]})


def enrich_ocr_coc(record, extraction, document_id, rows, evidence, money, warnings):
    """Join explicit CoC amounts with a named, reconciled payment schedule."""
    def key(name):
        return re.sub(r'[^a-z]', '', re.sub(r'\b(?:Limited|Ltd\.?)\b', '', name, flags=re.I).lower())
    named = {}
    for page in extraction['pages']:
        layout = page.get('ocr_layout_text', '')
        compact = re.sub(r'\s+', '', layout).lower()
        if not all(word in compact for word in ('creditor', 'claimed', 'admitted')):
            continue
        lines = layout.splitlines()
        for i, line in enumerate(lines):
            match = re.fullmatch(r'\s*\d+[.)]?\s+(.{3,100}?)\s{2,}(\d[\d,]*)\s{2,}(\d[\d,]*)\s{2,}\d+(?:\.\d+)?%\s*', line)
            if not match:
                continue
            name = re.sub(r'\s+', ' ', match[1]).strip()
            if i + 1 < len(lines) and re.fullmatch(r'\s*(?:Ltd\.?|Limited)\s*', lines[i+1], re.I):
                name += ' ' + lines[i+1].strip()
            name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
            named[key(name)] = {'creditor':name, 'claimed':money(match[2]),
                'admitted':money(match[3]), 'document_id':document_id, 'page':page['page']}
    if not named:
        return
    for row in named.values():
        if not any(key(f['value']) == key(row['creditor']) for f in record['financial_creditors']):
            record['financial_creditors'].append({'value':row['creditor'], 'document_id':document_id, 'page':row['page']})
    for page in extraction['pages']:
        layout = page.get('ocr_layout_text', '')
        compact = re.sub(r'\s+', '', layout).lower()
        security = re.search(r'(unsecured|secured)(?:financial|finanrial)', compact[:1000])
        if not security or not all(word in compact for word in ('proposed', 'payment', 'resolution', 'plan')):
            continue
        payment_category = security[1] + '_financial'
        payments = {}
        total = None
        for line in layout.splitlines():
            match = re.search(r'\s{2,}(\d{1,3}(?:[,.]\d{2,3})*[,.]\d{3})\s*$', line)
            if not match:
                continue
            amount = money(re.sub(r'[,.]', '', match[1]))
            name = re.split(r'\s{2,}', line[:match.start()].strip())[0]
            if name.lower() == 'total':
                total = amount
                continue
            nk = key(name)
            candidates = [k for k in named if k == nk or (len(nk) >= 6 and k.startswith(nk))]
            if not candidates and len(nk) > 10:
                # One dropped/repeated OCR character in an otherwise unique name.
                candidates = [k for k in named if abs(len(k)-len(nk)) == 1 and
                    any(k[:i]+k[i+1:] == nk for i in range(len(k))) or
                    abs(len(k)-len(nk)) == 1 and any(nk[:i]+nk[i+1:] == k for i in range(len(nk)))]
            if len(candidates) == 1:
                payments[candidates[0]] = amount
        if total is None or set(payments) != set(named) or sum(Decimal(v) for v in payments.values()) != Decimal(total):
            continue
        admitted_sum = sum(Decimal(row['admitted']) for row in named.values())
        aggregates = [r for r in rows if r['category'] == payment_category and r.get('scope') == 'category_total']
        if aggregates and any(r.get('admitted') is None or
                abs(Decimal(r['admitted'])-admitted_sum) > Decimal(len(named))/2 for r in aggregates):
            continue
        if aggregates:
            for aggregate in aggregates:
                rows.remove(aggregate)
                if Decimal(aggregate['admitted']) != admitted_sum:
                    warnings.append('Named creditor amounts are rounded in the source and differ from the stated category total; verify reconciliation.')
        # The named schedule replaces its category summary; displaying both
        # would count the same proposed allocation twice.
        record['plan_payments'] = [p for p in record['plan_payments'] if not (
            key(p['recipient']) == payment_category.replace('_', '') + 'creditors' and Decimal(p['amount']) == Decimal(total))]
        for nk, original in named.items():
            row = {**original, 'category':payment_category, 'scope':'individual',
                'plan_amount':payments[nk], 'notes':f'CoC amounts on page {original["page"]}; proposed payment schedule on page {page["page"]}. Actual payment is not stated.'}
            if not any(r['creditor'].casefold() == row['creditor'].casefold() for r in rows):
                rows.append(row)
            if not any(key(p['recipient']) == nk for p in record['plan_payments']):
                record['plan_payments'].append({'recipient':row['creditor'], 'amount':payments[nk],
                    'admitted':row['admitted'], 'document_id':document_id, 'page':page['page']})
            evidence.append({'field':'claims', 'page':row['page'], 'excerpt':row['creditor'] + ' / ' + row['claimed'] + ' / ' + row['admitted']})
            evidence.append({'field':'plan_payments', 'page':page['page'], 'excerpt':row['creditor'] + ' / ' + payments[nk]})


def enrich_verified_ocr_claims(record, extraction, document_id, rows, evidence, money):
    """Recover claimed/admitted rows only when a printed total reconciles.

    Some scanned tables merge adjacent monetary cells. Decimal boundaries
    remain usable even when no whitespace separates those cells. The third
    column here is explicitly under verification, never a proposed payment.
    """
    token = r'\d{1,3}(?:[,.]\d{2,3})*?\.\d{2}'
    row_pattern = re.compile(r'(?P<name>.*?)\s*(?P<claimed>' + token +
        r')\s*(?P<admitted>' + token + r')\s+0\.00\s+\d+(?:\.\d+)?\s*%?\s*')
    def amount(value):
        whole, cents = value.rsplit('.', 1)
        return money(whole.replace('.', ',') + '.' + cents)
    for page in extraction['pages']:
        layout = page.get('ocr_layout_text', '')
        header = re.search(r'List\s+of\s+(Operational|(?:Secured|Unsecured)\s+Financial)\s+Creditors', layout, re.I)
        compact = re.sub(r'\s+', '', layout).lower()
        if not header or not all(word in compact for word in ('claimed', 'admitted', 'verification', 'voting')):
            continue
        candidates = []
        total = None
        for line in layout[header.end():].splitlines():
            match = row_pattern.fullmatch(line)
            if not match:
                continue
            claimed, admitted = (amount(match[key]) for key in ('claimed', 'admitted'))
            name = match['name'].strip()
            name = re.sub(r'^(?:\d+[.)]?\s*|\[\s*)', '', name)
            name = re.sub(r'\s+n$', '', name).strip()
            if name.casefold() == 'total':
                total = claimed, admitted, line
                break
            name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
            # Common OCR confusion of the printed m with the glyph pair in.
            if re.fullmatch(r'Incoine\s*Tax\s*Department', name, re.I):
                name = 'Income Tax Department'
            if len(name) < 3 or claimed is None or admitted is None:
                continue
            category = ('government' if re.search(r'\b(?:tax|government|statutory)\b', name, re.I)
                        else 'operational' if header[1].casefold() == 'operational'
                        else 'unsecured_financial' if header[1].lower().startswith('unsecured')
                        else 'secured_financial')
            candidates.append({'creditor':name, 'category':category, 'scope':'individual',
                'claimed':claimed, 'admitted':admitted, 'plan_amount':None,
                'document_id':document_id, 'page':page['page'],
                'notes':'OCR row reconciled against the printed claimed/admitted totals; verify source.'})
        if not total or not candidates:
            continue
        if any(sum(Decimal(row[key]) for row in candidates) != Decimal(value)
               for key, value in zip(('claimed', 'admitted'), total[:2])):
            continue
        for row in candidates:
            if not any(existing['creditor'].casefold() == row['creditor'].casefold()
                       and existing['category'] == row['category'] for existing in rows):
                rows.append(row)
                evidence.append({'field':'claims', 'page':page['page'], 'excerpt':layout[header.start():][:1000]})
        for field, value in zip(('total_received_claims', 'total_admitted_claims'), total[:2]):
            if not record[field]:
                record[field]={'value':value, 'document_id':document_id, 'page':page['page']}
                evidence.append({'field':field, 'page':page['page'], 'excerpt':total[2]})


def enrich_ocr_rows(record, extraction, document_id, rows, evidence, money, category):
    # OCR sometimes renders Indian grouping commas as periods. Only accept the
    # complete grouped amount with exactly two decimal places.
    token = r'(?:\d[\d,]*\.\d{2}|\d{1,3}(?:\.\d{2,3}){2,}\.\d{2})'
    def amount(value):
        if value.count('.') > 1:
            whole, decimal = value.rsplit('.', 1)
            value = whole.replace('.', ',') + '.' + decimal
        return money(value)
    for page in extraction['pages']:
        layout = '\n\n'.join(dict.fromkeys(page.get(key, '') for key in
            ('layout_text', 'ocr_layout_text', 'embedded_layout_text') if page.get(key)))
        if not re.search(r'Total\s+claims', layout, re.I) or not re.search(r'admitted', layout, re.I) or not re.search(r'Resolution\s+Plan', layout, re.I):
            continue
        for match in re.finditer(r'^\s*((?:Secured|Unsecured)\s+Financial\s+Creditors)\s{2,}(' + token + r')\s{2,}(' + token + r')\s{2,}\d+(?:\.\d+)?%', layout, re.I | re.M):
            name, admitted, payment = match.groups()
            admitted, payment = amount(admitted), amount(payment)
            if admitted is None or payment is None:
                continue
            cat = category(name)
            if any(row['category'] == cat for row in rows):
                continue
            rows.append({'creditor': name, 'category': cat, 'scope': 'category_total',
                'claimed': None, 'admitted': admitted, 'plan_amount': payment,
                'document_id': document_id, 'page': page['page'],
                'notes': 'Complete OCR table row; verify against PDF. Proposed payment.'})
            if not any(item['recipient'].casefold() == name.casefold() for item in record['plan_payments']):
                record['plan_payments'].append({'recipient': name, 'amount': payment,
                    'admitted': admitted, 'document_id': document_id, 'page': page['page']})
            evidence.append({'field': 'claims', 'page': page['page'], 'excerpt': match.group(0)})
