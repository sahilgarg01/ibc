"""Shared field aliases and column roles, independent of PDF name or template."""
import re
import unicodedata


FIELD_ALIASES = {
    'company': (r'name of (?:the )?(?:cd|corporate debtor)', r'corporate debtor(?: name)?'),
    'cin': (r'(?:debtor |corporate debtor )?cin', r'corporate identification number'),
    'registered_office': (r'(?:debtor |corporate debtor )?registered office(?: address)?',),
    'industry': (r'(?:nature of )?business of (?:the )?(?:cd|corporate debtor)', r'industry|sector'),
    'case_number': (r'case (?:number|no)',),
    'tribunal': (r'tribunal|nclt bench|court',),
    'admission_date': (r'date of (?:initiation|commencement) of cirp', r'admission date|cirp commencement date'),
    'order_date': (r'order date|date of order',),
    'professional': (r'(?:name of (?:the )?)?(?:resolution professional|rp)',),
    'irp': (r'(?:name of (?:the )?)?(?:interim resolution professional|irp)',),
    'liquidator': (r'(?:name of (?:the )?)?liquidator',),
    'applicant': (r'name of (?:the )?successful resolution applicant(?: \(sra\))?', r'successful resolution applicant|resolution applicant'),
    'applicant_cin': (r'(?:successful )?resolution applicant cin|sra cin',),
    'applicant_incorporated': (r'(?:date of )?incorporation of (?:the )?(?:sra|resolution applicant)',),
    'applicant_location': (r'(?:registered office|address|location) of (?:the )?(?:sra|resolution applicant)',),
    'applicant_business': (r'nature of business of (?:the )?(?:sra|resolution applicant)',),
    'coc_voting_share': (r'voting percentage.*coc in favour of.*', r'coc voting (?:rights|share)'),
    'fair_value': (r'(?:(?:total|average) )?fair value(?: \(.*\))?',),
    'liquidation_value': (r'(?:(?:total|average) )?liquidation value(?: \(.*\))?',),
    'total_received_claims': (r'total (?:claims (?:received|filed)|received claims)(?: \(.*\))?',),
    'total_admitted_claims': (r'total (?:admitted claims|claims admitted)(?: \(.*\))?',),
}

LIST_ALIASES = {
    'previous_owners': r'(?:previous|former|erstwhile|pre[- ]resolution) (?:owners?|shareholders?|promoters?)',
    'current_owners': r'(?:current|present|existing) (?:owners?|shareholders?)',
    'subsequent_owners': r'(?:subsequent|post[- ]resolution) owners?',
    'previous_directors': r'(?:previous|former|erstwhile|suspended|pre[- ]resolution) (?:board of )?directors?',
    'current_directors': r'(?:current|present|existing) (?:board of )?directors?',
    'subsequent_directors': r'(?:subsequent|post[- ]resolution) directors?',
    'applicant_directors': r'(?:sra|resolution applicant) directors?',
}


def normalize_text(text):
    text = unicodedata.normalize('NFKC', text or '')
    return re.sub(r'[\u201c\u201d\u2018\u2019\ufffd]', '"', text).replace('\u00a0', ' ')


def clean_entity(value):
    value = ' '.join(normalize_text(value).split()).strip(' ,.;:"')
    return re.sub(r'^(?:(?:for|of|by)\s+|M/s\.?\s*|s\.\s*)+', '', value, flags=re.I)


def canonical_label(value):
    return re.sub(r'\s+', ' ', normalize_text(value)).strip(' .:;-').lower()


def match_field(label):
    label = canonical_label(label)
    for field, aliases in FIELD_ALIASES.items():
        if any(re.fullmatch(alias, label, re.I) for alias in aliases):
            return field
    return None


def column_role(value):
    if '%' in value or re.search(r'percentage|in plan to', value, re.I):
        return None
    key = re.sub(r'[^a-z]', '', value.lower())
    if key == 'claimed' or any(alias in key for alias in ('amountclaimed', 'claimsreceived', 'claimfiled', 'claimedamount')):
        return 'claimed'
    if key == 'admitted' or any(alias in key for alias in ('amountadmitted', 'claimsadmitted', 'admittedamount')):
        return 'admitted'
    if ('plan' in key and any(alias in key for alias in ('realizableamount', 'realisableamount', 'proposedpayment', 'amountunder', 'planamount'))) or key in {'proposedpayment', 'proposedamount', 'paymentunderplan', 'realizableamount', 'realisableamount', 'undertheplan'} or key.startswith('proposedpayment'):
        if 'percentage' not in key and 'claimed' not in key and 'admitted' not in key:
            return 'plan_amount'
    return None


def normalize_table(table):
    """Collapse extra grid boundaries whose adjacent cells never overlap."""
    if len(table) < 2:
        return table
    width = max((len(row) for row in table), default=0)
    cols = [[row[i] if i < len(row) else None for row in table] for i in range(width)]
    cols = [col for col in cols if any(cell and cell.strip() for cell in col)]
    i = 0
    while i + 1 < len(cols):
        if not any(a and a.strip() and b and b.strip() for a, b in zip(cols[i], cols[i+1])):
            cols[i] = [a or b for a, b in zip(cols[i], cols[i+1])]
            cols.pop(i+1)
        else:
            i += 1
    return [[col[i] for col in cols] for i in range(len(table))]


def labeled_values(page):
    """Read exact aliases from paragraph labels, spaced rows and table cells."""
    text = normalize_text(page.get('embedded_text', page['text']))
    for line in text.splitlines():
        parts = re.split(r'\s*[:\u2013\u2014]\s*|\s*-\s+|\s{2,}', line.strip(), maxsplit=1)
        if len(parts) == 2 and (field := match_field(parts[0])):
            yield field, parts[1], line
    for table in page.get('tables', []):
        for row in table:
            cells = [' '.join((cell or '').split()) for cell in row if cell and cell.strip()]
            for i, cell in enumerate(cells[:-1]):
                if field := match_field(cell):
                    yield field, cells[i+1], ' | '.join(cells)
