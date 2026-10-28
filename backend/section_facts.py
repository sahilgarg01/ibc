"""Map narrative roles and actual PDF table cells to evidence-backed case fields."""
import re
from decimal import Decimal
from .field_rules import FIELD_ALIASES, clean_entity, column_role, labeled_values, normalize_table, normalize_text
from .amount_units import UNIT_FACTORS, table_unit, value_unit


def compact(value):
    return ' '.join((value or '').split())


def table_money(value, money, unit='INR'):
    # A numeric cell may wrap its final digits onto the following line.
    raw = (value or '').strip()
    if '\n' in raw and re.search(r'\bnil\b', raw, re.I):
        parts = [part.strip() for part in raw.splitlines() if part.strip()]
        amounts = [Decimal(0) if part.casefold() == 'nil' else money(part, UNIT_FACTORS[unit]) for part in parts]
        return str(sum(Decimal(amount) for amount in amounts).quantize(Decimal('.01'))) if amounts and all(amount is not None for amount in amounts) else None
    value = re.sub(r'(?<=[\d,])\s+(?=\d)', '', (value or '').strip())
    value = re.sub(r'/\s*-$', '', value)
    if value.casefold() in {'nil', 'zero'}:
        return '0.00'
    result = money(value, UNIT_FACTORS[unit])
    if result is None and re.search(r'\bnil\b', value, re.I):
        parts = [part.strip() for part in value.splitlines() if part.strip()]
        amounts = [Decimal(0) if part.casefold() == 'nil' else money(part, UNIT_FACTORS[unit]) for part in parts]
        if amounts and all(amount is not None for amount in amounts):
            return str(sum(Decimal(amount) for amount in amounts).quantize(Decimal('.01')))
    return result


def valuation_amount(value, money, unit='INR'):
    """A cell can contain several valuers and a documented average."""
    text = compact(value)
    amount_pattern = r'([\d,]+(?:\.\d+)?(?:\s*(?:crores?|cr\.?|lakhs?|lacs?|millions?))?)'
    average = re.search(r'\bAverage\s*[:\-]?\s*(?:INR|Rs\.?)?\s*' + amount_pattern + r'(?:/\s*-)?', text, re.I)
    if average:
        return money(average.group(1), UNIT_FACTORS[unit]), 'Average'
    direct = table_money(value, money, unit)
    if direct is not None:
        return direct, 'Reported'
    amounts = [money(m.group(1), UNIT_FACTORS[unit]) for m in re.finditer(r'(?:INR|Rs\.?)\s*' + amount_pattern, text, re.I)]
    amounts = [Decimal(v) for v in amounts if v is not None]
    if len(amounts) > 1:
        return str((sum(amounts) / len(amounts)).quantize(Decimal('.01'))), 'Calculated average'
    if amounts:
        return str(amounts[0]), 'Reported'
    return None, None


def enrich_sections(record, extraction, document_id, put, evidence, money, iso_date):
    date = r'(\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})'
    company = r'([A-Za-z][A-Za-z0-9 &.,()\-]{2,130}?(?:Private Limited|Pvt\.?\s*Ltd\.?|Limited|LLP))'
    narrative_valuations = {}
    def add(field, value, page, excerpt):
        if not value:
            return
        if isinstance(record[field], list):
            if not any(item['value'].casefold() == value.casefold() for item in record[field]):
                record[field].append({'value': value, 'document_id': document_id, 'page': page})
                evidence.append({'field': field, 'page': page, 'excerpt': excerpt[:1000]})
        else:
            put(field, value, page, excerpt)

    for page in extraction['pages']:
        n = page['page']
        flat = compact(normalize_text(page.get('embedded_text', page['text'])))
        for field, value, excerpt in labeled_values(page):
            if field in {'fair_value', 'liquidation_value'}:
                if ' | ' not in excerpt:
                    source_unit = value_unit(value, table_unit(page, []))
                    amount, basis = valuation_amount(value, money, source_unit)
                    if amount is not None:
                        narrative_valuations[field] = (amount, n, excerpt, basis, source_unit)
            elif field in {'total_received_claims', 'total_admitted_claims'}:
                add(field, table_money(value, money), n, excerpt)
            elif field.endswith('_date') or field == 'applicant_incorporated':
                add(field, iso_date(value), n, excerpt)
            elif field in {'company', 'applicant', 'professional', 'liquidator', 'irp'}:
                add(field, clean_entity(value), n, excerpt)
            elif field in {'cin', 'applicant_cin'}:
                cin = re.sub(r'\s+', '', value).upper()
                scoped = field == 'applicant_cin' or re.search(r'\bdebtor\b', excerpt, re.I) or (n <= 3 and 'corporate debtor' in flat.lower())
                if scoped and re.fullmatch(r'[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}', cin):
                    add(field, cin, n, excerpt)
            elif field not in {'applicant_cin'}:
                add(field, value[:500], n, excerpt)
        # Explicit definition of the debtor takes precedence over a truncated caption.
        if n <= 3:
            m = re.search(company + r'\s*\(["\u201c\u201d\s]*(?:Corporate Debtor|CD)\b', flat, re.I)
            if m:
                value = clean_entity(re.split(r'\b(?:of|by|for)\s+(?:M/s\.?\s*)?', m.group(1), flags=re.I)[-1])
                add('company', value, n, m.group(0))
            m = re.search(r'Order\s+(?:Delivered|Pronounced)\s*(?:On)?\s*:?\s*' + date, flat, re.I)
            if m:
                add('order_date', iso_date(m.group(1)), n, m.group(0))
            # Both individuals and insolvency professional entities may act as RP.
            m = re.search(r'(?:filed\s+by|appoint)\s+(?:Resolution Professional\s+)?(?:M/s\.?\s*)?' + company + r'\s*[, ("\u201c]*(?:Applicant["\u201d\s/]*)?Resolution\s+Professional', flat, re.I)
            if not m:
                m = re.search(r'Resolution Professional\s+(?:M/s\.?\s*)?' + company + r'\s*\(', flat, re.I)
            if m:
                add('professional', compact(m.group(1)).rstrip(','), n, m.group(0))
            m = re.search(r'filed by\s+(?:Resolution Professional\s+)?' + company + r'\s+through\s+(?:Mr\.?|Ms\.?)\s+', flat, re.I)
            if m:
                add('professional', clean_entity(m.group(1)), n, m.group(0))
            m = re.search(r'submitted\s+by\s+(?:Mr\.?\s+|M/s\.?\s*)?(.{3,130}?)\s*\((?:hereinafter referred to as the\s+)?["\s]*(?:Successful\s+Resolution\s+Applicant|Respondent/SRA|SRA)', flat, re.I)
            if m and not re.fullmatch(r'(?:the\s+)?(?:Successful\s+)?Resolution\s+Applicant', m.group(1).strip(), re.I):
                applicant_name = clean_entity(m.group(1)).split('"')[0].rstrip(' ,')
                add('applicant', applicant_name, n, m.group(0))
                add('plan_submitters', applicant_name, n, m.group(0))
            m = re.search(r'\bC\.?\s*P\.?\s*\(\s*I\.?\s*B\.?\s*\)\s*(?:No\.?\s*)?/?\s*\d+\s*(?:\([A-Z]+\)|/[A-Z]+/|/|of\s+)\s*\d{4}\b', flat, re.I)
            if m and not re.search(r'IN THE HIGH COURT', extraction['pages'][0]['text'][:1200], re.I):
                add('case_number', m.group(0), n, m.group(0))
        # Only the paragraph explicitly describing the corporate debtor supplies its CIN/address.
        for m in re.finditer(r'\bThe Corporate Debtor is\b(.{0,950}?)(?=\s+\d{1,2}\.\s|$)', flat, re.I):
            paragraph = m.group(0)
            cin = re.search(r'\bCIN\s*\)?\s*[:\-]?\s*([LU](?:\s*[A-Z0-9]){20})\b', paragraph, re.I)
            if cin:
                value = re.sub(r'\s+', '', cin.group(1)).upper()
                if re.fullmatch(r'[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}', value):
                    add('cin', value, n, paragraph)
            address = re.search(r'(?:Its\s+)?registered office\s+is\s+(?:situated\s+)?at\s+(.{5,450}?)(?=\.\s+[A-Z]|\.$|$)', paragraph, re.I)
            if address:
                add('registered_office', address.group(1).rstrip('.'), n, address.group(0))
        m = re.search(r'Corporate Debtor is\s+(?:primarily\s+)?engaged in\s+(?:the business of\s+)?(.{5,250}?)(?:\.\s|$)', flat, re.I)
        if m:
            add('industry', m.group(1).rstrip('.'), n, m.group(0))
        m = re.search(r'(?:CIRP|Corporate Insolvency Resolution Process).{0,140}?commenced\s+(?:pursuant\s+to\s+the\s+order\s+dated\s+)' + date, flat, re.I)
        if m:
            add('admission_date', iso_date(m.group(1)), n, m.group(0))
        m = re.search(r'(?:Mr\.?\s+)([A-Z][A-Za-z .]{3,80}?),\s*Suspended Director of the Corporate Debtor', flat, re.I)
        if m:
            add('previous_directors', m.group(1), n, m.group(0))
        if not record['applicant']:
            m = re.search(r'(?:Mr\.?|Ms\.?|Dr\.?|M/s\.?)\s*([A-Za-z][A-Za-z0-9 .&-]{2,100}?)\s*\((?:hereinafter referred to as\s+(?:the\s+)?)?["\s]*(?:Successful Resolution Applicant|Resolution Applicant|SRA|RA)(?:["\s]+or\s+["\s]*RA)?["\s]*\)', flat, re.I)
            if m:
                add('applicant', clean_entity(m.group(1)), n, m.group(0))
        m = re.search(r'Successful Resolution Applicant\s*,\s*namely\s+(.{3,150}?)(?=,\s*(?:is|has|was|being)\b)', flat, re.I)
        if m:
            add('applicant', clean_entity(m.group(1)), n, m.group(0))
        if not record['company']:
            m = re.search(r'Corporate Debtor\s*,\s*(?:namely\s*,?\s*)?(?:M/s\.?\s*)?' + company, flat, re.I)
            if m:
                add('company', clean_entity(m.group(1)), n, m.group(0))
        m = re.search(r'Corporate Debtor was admitted into CIRP\s+(?:vide\s+)?Order\s+dated\s*' + date, flat, re.I)
        if m:
            add('admission_date', iso_date(m.group(1)), n, m.group(0))
        for m in re.finditer(r'(?:Resolution Plan (?:was|has been) (?:voted|approved) with|CoC.{0,50}?with)\s*(\d{1,3}(?:\.\d+)?)\s*%\s*(?:votes|voting)', flat, re.I):
            add('coc_voting_share', m.group(1) + '%', n, m.group(0))
        # Explicit proposed allotments remain distinct from implemented ownership.
        m = re.search(r'(?:shall|will)\s+(?:issue\s+and\s+)?allot\s+equity\s+shares?\s+to\s+(?:Mr\.?\s+|M/s\.?\s*)?(.{3,100}?)(?=,\s*Resolution Applicant|\s+in consideration|\.)', flat, re.I)
        if m:
            add('proposed_owners', clean_entity(m.group(1)), n, m.group(0))
        for m in re.finditer(r'\b(?:Mr|Ms|Mrs|Dr)\.?\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){1,4})\s*,?\s*(?:who\s+(?:is|was)\s+(?:also\s+)?(?:the\s+)?|one of the\s+)(?:Suspended|Erstwhile)\s+Directors?\s+of\s+the\s+Corporate\s+Debtor', flat):
            add('previous_directors', m.group(1), n, m.group(0))

    # Form H and annexures are label/value tables, independent of their line wrapping.
    aliases = {field: '(?:' + '|'.join(patterns) + ')' for field, patterns in FIELD_ALIASES.items()
               if field not in {'cin', 'applicant_cin', 'fair_value', 'liquidation_value', 'total_received_claims', 'total_admitted_claims'}}
    valuation = {}
    plan_value = None
    admitted_total = None
    for page in extraction['pages']:
        n = page['page']
        for table in page.get('tables', []):
            for row in table:
                cells = [compact(c) for c in row if compact(c)]
                if len(cells) < 2:
                    continue
                for field, label in aliases.items():
                    for i, cell in enumerate(cells[:-1]):
                        if re.fullmatch(label, cell, re.I):
                            value = cells[i+1]
                            if field.endswith('_date'):
                                value = iso_date(value)
                            if field in {'company', 'applicant', 'professional'}:
                                value = clean_entity(value)
                            if field == 'industry':
                                business = re.search(r'engaged in (?:the )?(.+?)(?:\.\s|$)', value, re.I)
                                if business:
                                    value = business.group(1)
                            add(field, value[:500] if value else None, n, ' | '.join(cells))
                for i, cell in enumerate(cells[:-1]):
                    field = {'fair value': 'fair_value', 'liquidation value': 'liquidation_value'}.get(cell.casefold())
                    if field:
                        source_unit = value_unit(cells[i+1], table_unit(page, table))
                        value, basis = valuation_amount(cells[i+1], money, source_unit)
                        if value is not None:
                            # Prefer later, more precise Form H summary amounts over rounded entries.
                            if not (field in valuation and valuation[field][3] == 'Average' and valuation[field][0] == value):
                                valuation[field] = (value, n, ' | '.join(cells), basis, source_unit)
                    if re.match(r'^(?:Resolution Plan Value|Total Realisable amount under the plan)\b', cell, re.I):
                        amount = re.match(r'(?:INR\s*|Rs\.?\s*)?([\d,]+(?:\.\d+)?)', cells[i+1])
                        if amount and money(amount.group(1)):
                            plan_value = (money(amount.group(1)), n, ' | '.join(cells))
                header = compact(' '.join(' '.join(c or '' for c in r) for r in table[:1])).lower()
                if 'name of creditor' in header and 'voting' in header and len(cells) >= 3 and re.fullmatch(r'\d+\.?', cells[0]) and '%' in cells[2]:
                    add('financial_creditors', cells[1], n, ' | '.join(cells))
                if len(cells) == 2 and re.fullmatch(r'(?:Towards CIRP cost|Operational Creditors.*|Sole Financial Creditor)', cells[0], re.I):
                    value = table_money(cells[1], money)
                    if value is not None and not any(p['recipient'].casefold() == cells[0].casefold() for p in record['plan_payments']):
                        record['plan_payments'].append({'recipient': cells[0], 'amount': value, 'admitted': None,
                            'document_id': document_id, 'page': n})
                        evidence.append({'field': 'plan_payments', 'page': n, 'excerpt': ' | '.join(cells)})
                if 'principal' in header and 'total' in header and any(c.casefold() == 'total' for c in cells[:-1]):
                    value = table_money(cells[-1], money)
                    if value is not None:
                        admitted_total = (value, n, ' | '.join(cells))
    for field, value in narrative_valuations.items():
        valuation.setdefault(field, value)
    for field, (value, n, excerpt, basis, source_unit) in valuation.items():
        add(field, value, n, excerpt)
    if valuation and not record['asset_values']:
        n = min(item[1] for item in valuation.values())
        basis = 'Average' if all(v[3] == 'Average' for v in valuation.values()) else 'Calculated average' if any(v[3] == 'Calculated average' for v in valuation.values()) else 'Reported'
        record['asset_values'].append({'asset': 'Corporate debtor (total)', 'basis': basis,
            'fair_value': valuation.get('fair_value', (None,))[0],
            'liquidation_value': valuation.get('liquidation_value', (None,))[0],
            'document_id': document_id, 'page': n,
            'fair_value_page': valuation.get('fair_value', (None, n))[1],
            'liquidation_value_page': valuation.get('liquidation_value', (None, n))[1],
            'fair_value_source_unit': valuation.get('fair_value', (None, n, '', '', 'INR'))[4],
            'liquidation_value_source_unit': valuation.get('liquidation_value', (None, n, '', '', 'INR'))[4]})
        evidence.append({'field': 'asset_values', 'page': n, 'excerpt': ' | '.join(v[2] for v in valuation.values())[:1000]})
    if admitted_total:
        add('total_admitted_claims', admitted_total[0], admitted_total[1], admitted_total[2])
    if plan_value and record['applicant'] and not record['plans']:
        record['plans'].append({'applicant': record['applicant']['value'], 'amount': plan_value[0],
            'admitted': admitted_total[0] if admitted_total and Decimal(admitted_total[0]) > 0 else None,
            'document_id': document_id, 'page': plan_value[1]})
        evidence.append({'field': 'plans', 'page': plan_value[1], 'excerpt': plan_value[2][:1000]})
    # Narrative valuation labels need the same asset card as table-based values.
    if not record['asset_values'] and (record['fair_value'] or record['liquidation_value']):
        fair, liquidation = record['fair_value'], record['liquidation_value']
        source = fair or liquidation
        record['asset_values'].append({'asset': 'Corporate debtor (total)', 'basis': 'Reported',
            'fair_value': fair['value'] if fair else None, 'liquidation_value': liquidation['value'] if liquidation else None,
            'document_id': document_id, 'page': source['page'],
            'fair_value_page': fair['page'] if fair else None, 'liquidation_value_page': liquidation['page'] if liquidation else None})


def enrich_asset_tables(record, extraction, document_id, evidence, money):
    """Read any asset/valuer table that explicitly labels fair and liquidation columns."""
    rows = []
    columns = None
    last_page = None
    unit = 'INR'
    subtotal_groups = set()
    for page in extraction['pages']:
        if last_page is not None and page['page'] > last_page + 1:
            columns = None
            subtotal_groups.clear()
        text = compact(normalize_text(page['text']))
        for table in page.get('tables', []):
            asset = None
            for row in table:
                cells = [compact(cell) for cell in row]
                keys = [re.sub(r'[^a-z]', '', cell.lower()) for cell in cells]
                fair = next((i for i, key in enumerate(keys) if key in {'fairvalue', 'fairmarketvalue'}), None)
                liquidation = next((i for i, key in enumerate(keys) if key == 'liquidationvalue'), None)
                if fair is not None and liquidation is not None:
                    ai = next((i for i, key in enumerate(keys) if key in {'asset', 'assetclass', 'assetdescription', 'propertydescription', 'item', 'particulars', 'description'}), 0)
                    vi = next((i for i, key in enumerate(keys) if 'valuer' in key), None)
                    average_fair = next((i for i, key in enumerate(keys) if key == 'averagefairvalue'), None)
                    average_liquidation = next((i for i, key in enumerate(keys) if key == 'averageliquidationvalue'), None)
                    columns = ai, vi, average_fair if average_fair is not None else fair, average_liquidation if average_liquidation is not None else liquidation
                    previous_page = next((p for p in extraction['pages'] if p['page'] == page['page']-1), None)
                    unit = table_unit(page, table, previous_page)
                    last_page = page['page']
                    continue
                if columns is None or len(cells) <= max(i for i in columns if i is not None):
                    continue
                ai, vi, fi, li = columns
                valuer_row = re.match(r'^Valuer\s*\d+\b', cells[ai], re.I)
                if valuer_row:
                    # A valuer name is the valuation basis, not an asset class.
                    context = re.search(r'valued\s+the\s+(.{3,100}?),\s*and\s+their\s+reports', text, re.I)
                    asset = context.group(1) if context else 'Corporate debtor (total)'
                elif cells[ai] and not cells[ai].isdigit() and cells[ai].casefold() != 'average':
                    asset = cells[ai]
                fv, lv = table_money(cells[fi], money, unit), table_money(cells[li], money, unit)
                if not asset or (fv is None and lv is None):
                    continue
                row_label = ' '.join(cells[:min(fi, li)]).strip()
                subtotal = re.match(r'^\(?([A-Z])\.?\)?\s*[.:-]?\s*Total\b', row_label, re.I)
                if subtotal:
                    subtotal_groups.add(subtotal.group(1).upper())
                summary_average = bool(re.match(r'^Average\s+(?:Total|Valuation)\b', row_label, re.I)
                    or (len(subtotal_groups) >= 2 and re.match(r'^Average\s+of\b', row_label, re.I)))
                total_row = any(re.fullmatch(r'(?:grand\s+)?total', cell, re.I) for cell in cells[:min(fi, li)])
                if total_row or summary_average or re.search(r'\btotal\b|average of', asset, re.I):
                    if total_row or summary_average:
                        for field, amount in (('fair_value', fv), ('liquidation_value', lv)):
                            if amount is not None:
                                record[field] = {'value': amount, 'document_id': document_id, 'page': page['page']}
                                evidence.append({'field': field, 'page': page['page'], 'excerpt': ' | '.join(cells)[:1000]})
                    continue
                basis = cells[ai] if valuer_row else cells[vi] if vi is not None and cells[vi] else 'Reported'
                if re.search(r'\baverage\b', ' '.join(cells[:min(fi, li)]), re.I) or average_fair is not None or average_liquidation is not None:
                    basis = 'Average'
                rows.append({'asset': asset[:150], 'basis': basis[:100], 'fair_value': fv, 'liquidation_value': lv,
                    'document_id': document_id, 'page': page['page'],
                    'fair_value_source_unit': value_unit(cells[fi], unit),
                    'liquidation_value_source_unit': value_unit(cells[li], unit)})
                evidence.append({'field': 'asset_values', 'page': page['page'], 'excerpt': ' | '.join(cells)[:1000]})
                last_page = page['page']
            # Shareholding tables supply names only when their time period is explicit.
            heading = ' '.join(compact(c) for row in table[:2] for c in row)
            if re.search(r'name of (?:the )?(?:shareholder|director)', heading, re.I):
                role = 'owners' if 'shareholder' in heading.lower() else 'directors'
                period = 'proposed' if re.search(r'proposed|post[- ](?:resolution|infusion)|capital structure.{0,100}will be', text, re.I) else 'current' if re.search(r'(?:existing|current|present)\s+(?:shareholders?|shareholding|directors?)', text, re.I) else None
                if period:
                    for row in table[1:]:
                        name = clean_entity(next((c for c in row if c and not re.fullmatch(r'\d+[.)]?', c.strip())), ''))
                        if 3 <= len(name) <= 120 and not re.search(r'total|shareholder|director', name, re.I):
                            field = period + '_' + role
                            if not any(v['value'].casefold() == name.casefold() for v in record[field]):
                                record[field].append({'value': name, 'document_id': document_id, 'page': page['page']})
                                evidence.append({'field': field, 'page': page['page'], 'excerpt': ' | '.join(c or '' for c in row)[:1000]})
    if rows:
        # An aggregate card is displayed only when no detailed asset breakdown exists.
        existing = [v for v in record['asset_values'] if v['asset'] != 'Corporate debtor (total)']
        seen = {(v['asset'].casefold(), v['basis'].casefold(), v['fair_value'], v['liquidation_value']) for v in existing}
        for row in rows:
            key = row['asset'].casefold(), row['basis'].casefold(), row['fair_value'], row['liquidation_value']
            if key not in seen:
                existing.append(row)
                seen.add(key)
        record['asset_values'] = existing


def enrich_claim_tables(record, extraction, document_id, claim_rows, evidence, money, category):
    """Carry explicit claim column roles and category headings across continued tables."""
    columns = None
    last_page = None
    current_category = None
    grand_total = None
    scale = Decimal(1)
    derive_payments = not record['plan_payments']
    pending_roles = {}
    name_column = 0
    coc_table = False
    for page in extraction['pages']:
        n = page['page']
        if last_page is not None and n - last_page > 1:
            columns = None
            pending_roles = {}
            current_category = None
        for raw_table in page.get('tables', []):
            table = normalize_table(raw_table)
            if max((len(row) for row in table), default=0) < 3:
                continue
            first_body = next((j for j, row in enumerate(table) if any(
                table_money(cell or '', money) is not None for cell in row[1:])), min(7, len(table)))
            header = [' '.join(compact(row[i] if i < len(row) else '') for row in table[:first_body]).lower()
                      for i in range(max((len(row) for row in table), default=0))]
            roles = {}
            for i, cell in enumerate(header):
                role = column_role(cell)
                if role:
                    roles.setdefault(role, i)
            claimed, admitted, payment = (roles.get(key) for key in ('claimed', 'admitted', 'plan_amount'))
            if roles:
                name_header = r'name of (?:the )?(?:members|(?:financial\s+)?creditors?)'
                coc_table = any(re.search(name_header, cell, re.I) for cell in header) and any('voting' in cell for cell in header)
                pending_roles.update(roles)
                for i, cell in enumerate(header):
                    if re.search(r'stakeholder|category of claims|' + name_header, cell, re.I):
                        name_column = i
                claimed, admitted, payment = (pending_roles.get(key) for key in ('claimed', 'admitted', 'plan_amount'))
            if admitted is not None and (claimed is not None or payment is not None):
                columns = (claimed, admitted, payment)
                last_page = n
                previous_page = next((p for p in extraction['pages'] if p['page'] == n-1), {})
                scale = UNIT_FACTORS[table_unit(page, table, previous_page)]
            elif any(re.search(r'particulars|no\. of shares|means of finance|fair\s+value|liquidation\s+value|name of (?:the )?valuers', compact(cell), re.I) for row in table[:2] for cell in row):
                # A new unrelated table ends the continued creditor table.
                columns = None
                pending_roles = {}
            if columns is None or max(i for i in columns if i is not None) >= max((len(row) for row in table), default=0):
                continue
            for row in table:
                name = compact(row[name_column] if name_column < len(row) else '')
                if not name:
                    name = next((compact(c) for c in row if c and re.search(r'(?:secured|unsecured|operational)\s+(?:financial\s+)?creditors?', c, re.I)), '')
                if not name:
                    continue
                if re.search(r'\bshareholders?\b', name, re.I):
                    continue
                values = [table_money(row[i] if i is not None and i < len(row) else '', money) for i in columns]
                values = [str((Decimal(v) * scale).quantize(Decimal('.01'))) if v is not None else None for v in values]
                if all(value is None for value in values):
                    if re.match(r'(?:(?:Unrelated|Related)\s+)?(?:(?:Secured|Unsecured)(?: Financial)? Creditors?|Operational Creditors)', name, re.I):
                        current_category = category(name) if 'financial' in name.lower() or 'operational' in name.lower() else 'unsecured_financial' if 'unsecured' in name.lower() else 'secured_financial'
                    continue
                candidate_category = category(name) if re.search(r'financial\s+creditors', name, re.I) else current_category
                if re.search(r'creditors not having.*dissenting.*assenting', name, re.I) and any(
                    prior['category'] == candidate_category and prior.get('scope') == 'category_total'
                    for prior in claim_rows):
                    continue
                last_page = n
                excerpt = ' | '.join(compact(cell) for cell in row)
                if re.fullmatch(r'(?:grand\s+)?total(?: resolution plan amount)?', name, re.I):
                    # Defer: category subtotals can precede the final grand total.
                    grand_total = (values, n, excerpt)
                    continue
                normalized = re.sub(r'[^a-z]', '', name.lower())
                is_cost = 'cirpcost' in normalized or 'insolvencyresolutionprocesscost' in normalized
                if re.search(r'financial\s+creditors?|operational\s+creditors?|government|workmen|employee|statutory', name, re.I):
                    current_category = category(name)
                elif re.fullmatch(r'Unsecured Creditors', name, re.I):
                    current_category = 'other'
                elif re.search(r'other debts|contingent liabilities', name, re.I):
                    current_category = 'other'
                if derive_payments and values[2] is not None and not any(item['recipient'].casefold() == name.casefold() for item in record['plan_payments']):
                    record['plan_payments'].append({'recipient': name[:200], 'amount': values[2],
                        'admitted': values[1], 'document_id': document_id, 'page': n})
                    evidence.append({'field': 'plan_payments', 'page': n, 'excerpt': excerpt[:1000]})
                if is_cost:
                    continue
                if current_category is None:
                    if coc_table:
                        current_category = 'other'
                    else:
                        continue
                row_category = 'government' if 'government' in normalized else 'employee' if any(s in normalized for s in ('workmen', 'employees')) else current_category
                if re.match(r'Operational Creditors?\b', name, re.I) and 'government' in normalized and 'workmen' in normalized:
                    row_category = 'operational'
                scope = 'category_total' if name.startswith('-') or any(s in normalized for s in ('government', 'operationalcreditors', 'financialcreditors', 'workmen', 'employees')) else 'individual'
                creditor = name
                if name.lstrip('- ').casefold() in {'assenting', 'dissenting'}:
                    creditor = row_category.replace('_', ' ').title() + ' creditors (' + name.lstrip('- ') + ')'
                claim_rows.append({'creditor': creditor, 'category': row_category, 'scope': scope,
                    'claimed': values[0], 'admitted': values[1], 'plan_amount': values[2],
                    'document_id': document_id, 'page': n,
                    'notes': 'Extracted from PDF table cells; proposed recovery, not actual payment.'})
                evidence.append({'field': 'claims', 'page': n, 'excerpt': excerpt[:1000]})
                if (coc_table or row_category in {'secured_financial', 'unsecured_financial'}) and scope == 'individual' and not any(v['value'].casefold() == creditor.casefold() for v in record['financial_creditors']):
                    record['financial_creditors'].append({'value': creditor, 'document_id': document_id, 'page': n})
    if grand_total:
        values, n, excerpt = grand_total
        # Accept totals only when the extracted breakdown reconciles exactly.
        for field, amount_field, value in zip(('total_received_claims', 'total_admitted_claims'), ('claimed', 'admitted'), values):
            rows = [row.get(amount_field) for row in claim_rows]
            if value is not None and rows and all(v is not None for v in rows) and sum(Decimal(v) for v in rows) == Decimal(value) and not record[field]:
                record[field] = {'value': value, 'document_id': document_id, 'page': n}
                evidence.append({'field': field, 'page': n, 'excerpt': excerpt[:1000]})
        # Explicit totals are also valid when some constituent claims remain unknown.
        for field, value in zip(('total_received_claims', 'total_admitted_claims'), values):
            if value is not None and not record[field] and len(record['plan_payments']) > 1:
                record[field] = {'value': value, 'document_id': document_id, 'page': n}
                evidence.append({'field': field, 'page': n, 'excerpt': excerpt[:1000]})
        if values[2] is not None and record['applicant'] and not record['plans']:
            record['plans'].append({'applicant': record['applicant']['value'], 'amount': values[2],
                'admitted': values[1] if values[1] is not None and Decimal(values[1]) > 0 else None,
                'document_id': document_id, 'page': n})
            evidence.append({'field': 'plans', 'page': n, 'excerpt': excerpt[:1000]})
