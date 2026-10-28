"""Evidence-based party lists, including numbered lists continued across pages."""
import re
from .field_rules import clean_entity, normalize_text


def coram_names(page):
    """Read the court's Coram block, retaining layout positions for wrapped names."""
    text = normalize_text(page.get('embedded_layout_text', page.get('layout_text', page['text'])))
    heading = re.search(r'^\s*C\s*O\s*R\s*A\s*M\s*[:\-]*\s*', text, re.I | re.M)
    if not heading or len(' '.join(text[:heading.start()].split())) > 1800:
        return []
    block = text[heading.end():heading.end()+1400]
    block = re.split(r'^\s*(?:PRESENT\s*:|Appearances?\b|For the\b|O\s*R\s*D\s*E\s*R\b|Per\s*:)', block, maxsplit=1, flags=re.I | re.M)[0]
    entries = []
    for line in block.splitlines():
        prefixes = list(re.finditer(r'\b(?:Shri\.?|Sh\.|Justice|Mrs\.?|Ms\.?|Mr\.?)\s+', line, re.I))
        if prefixes:
            for i, prefix in enumerate(prefixes):
                end = prefixes[i+1].start() if i+1 < len(prefixes) else len(line)
                name = re.split(r'Hon[\w\W]{0,5}ble|\bMember\b', line[prefix.end():end], maxsplit=1, flags=re.I)[0].strip(' ,.;:-')
                if re.fullmatch(r'[A-Za-z][A-Za-z .\-]{2,100}', name):
                    entries.append({'column': prefix.start(), 'parts': [name]})
        elif entries:
            for segment in re.finditer(r'\S(?:.*?\S)?(?=\s{2,}|$)', line):
                value = segment.group().strip()
                if re.fullmatch(r'[A-Za-z][A-Za-z .\-]{1,50}', value) and not re.search(r'\b(?:member|hon|technical|judicial|coram)\b', value, re.I):
                    target = min(entries, key=lambda entry: abs(entry['column']-segment.start()))
                    # Native text can move a right-column surname to column zero.
                    if segment.start() == 0 and len(entries) > 1 and len(value.split()) == 1:
                        target = entries[-1]
                    target['parts'].append(value)
    result = []
    for entry in entries:
        name = ' '.join(' '.join(entry['parts']).split())
        if len(name.split()) >= 2:
            result.append((name.title() if name.isupper() else name, block))
    return result


def enrich_parties(record, extraction, document_id, evidence, iso_date):
    def add(field, value, page, excerpt):
        value = clean_entity(value)
        if not 2 <= len(value) <= 500:
            return
        fact = {'value': value, 'document_id': document_id, 'page': page}
        if isinstance(record[field], list):
            if field == 'judges':
                judge_key = lambda name: re.split(r'\s*[-,]?\s*Member\b', name, maxsplit=1, flags=re.I)[0].strip().casefold()
                if any(judge_key(item['value']) == judge_key(value) for item in record[field]):
                    return
            if any(item['value'].casefold() == value.casefold() for item in record[field]):
                return
            record[field].append(fact)
        elif record[field]:
            return
        else:
            record[field] = fact
        evidence.append({'field': field, 'page': page, 'excerpt': excerpt[:1000]})

    active = False
    item = None
    def flush():
        nonlocal item
        if item:
            add('plan_submitters', item['value'], item['page'], item['value'])
            item = None

    for page in extraction['pages']:
        for name, excerpt in coram_names(page):
            add('judges', name, page['page'], excerpt)
        text = normalize_text(page.get('embedded_text', page['text']))
        flat = ' '.join(text.split())
        creditors = re.search(r'claims\s+were\s+received\s+from\s+(?:the\s+)?(.{3,200}?)\s+as\s+(?:Secured|Unsecured)\s+Financial\s+Creditors\b', flat, re.I)
        if creditors:
            for name in re.split(r',\s*|\s+and\s+', creditors[1], flags=re.I):
                add('financial_creditors', name, page['page'], creditors.group())
        if page['page'] <= 3:
            caption_creditor = re.search(r'(?m)^\s*([A-Z][A-Za-z0-9 &.,-]{2,100})\s*\n(?:Having\s+its\s+(?:registered\s+)?(?:address|office)\s+at\s*:\s*[\s\S]{5,350}?)?[.…\s]*(?:Petitioner\s*/\s*)?Financial\s+Creditor\b', text, re.I)
            if caption_creditor:
                add('financial_creditors', caption_creditor.group(1), page['page'], caption_creditor.group())
        creditor = re.search(r'\bclaim\s+was\s+received\s+from\s+(.{3,120}?)\s+as\s+the\s+sole\s+Financial\s+Creditor\b', flat, re.I)
        if creditor:
            add('financial_creditors', creditor.group(1), page['page'], creditor.group())
        for line in text.splitlines():
            line = line.strip()
            if re.search(r'(?:Resolution )?Plans?\s+from the following|Plans? received from the following', line, re.I):
                active = True
                continue
            if not active or not line:
                continue
            # Page headers and footers are not part of a party name.
            if re.search(r'^(?:Page\s+\d|I\.?A\b|NATIONAL COMPANY|IN THE NATIONAL)', line, re.I):
                continue
            entry = re.match(r'^[ivxlcdm]+[.)]\s+(.+)', line, re.I)
            if entry:
                flush()
                item = {'value': entry.group(1), 'page': page['page']}
            elif re.match(r'^\d+[.]\s', line):
                flush()
                active = False
            elif item:
                item['value'] += ' ' + line
        if not record['applicant_business']:
            group = re.search(r'\b(?:The\s+)?SRA\s+is\s+(part\s+of\s+.{5,240}?)(?:\.\s|\.$|$)', flat, re.I)
            if group:
                add('applicant_business', group[1], page['page'], group.group())
            profile = re.search(r'(?:The )?Resolution Applicant is\s+(?:an?\s+)?((?!(?:entitled|permitted|required|allowed|eligible|liable|directed|requested|obliged)\b)[^.;]{5,200})[.;]', flat, re.I)
            if profile:
                add('applicant_business', profile.group(1), page['page'], profile.group(0))
        voted = re.search(r'resolution plans? of\s+(.{5,250}?)\s+were put to vote', flat, re.I)
        if voted:
            for name in re.split(r'\s+and\s+', voted.group(1), flags=re.I):
                add('plan_submitters', re.sub(r'^(?:Mr|Ms|Mrs)\.?\s+', '', name), page['page'], voted.group(0))
        profile = re.search(r'Resolution Applicant.{0,1000}?registered in\s+([A-Za-z ]{3,60}?)\s+on\s+(\d{1,2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{4})', flat, re.I)
        if profile:
            add('applicant_location', profile.group(1), page['page'], profile.group(0))
            date = iso_date(re.sub(r'\s+', '', profile.group(2)))
            if date:
                add('applicant_incorporated', date, page['page'], profile.group(0))
        for party in record['plan_submitters']:
            name = re.escape(party['value'])
            withdrawn = re.search(name + r'.{0,100}?\b(?:withdrew|withdrawn|withdraw)\b.{0,60}?\b(?:resolution plan|bid)\b', flat, re.I)
            if withdrawn:
                add('withdrawn_submitters', party['value'], page['page'], withdrawn.group(0))
    flush()
    if record['applicant']:
        applicant = record['applicant']
        add('plan_submitters', applicant['value'], applicant['page'], 'Successful resolution applicant: ' + applicant['value'])
        pages = extraction['pages']
        for i, page in enumerate(pages):
            following = pages[i+1] if i+1 < len(pages) else None
            joined = ' '.join((page['text'] + '\n' + (following['text'] if following else '')).split())
            proposed = re.search(r'on\s+and\s+from\s+the\s+Closing\s+date,\s+the\s+SRA\b.{0,600}?shall\s+hold\s+100\s*%\s+shareholding\b', joined, re.I)
            if proposed:
                source_page = following['page'] if following and proposed.start() >= len(' '.join(page['text'].split())) else page['page']
                add('proposed_owners', applicant['value'], source_page, proposed.group())
                break
