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
    block = re.split(r'^\s*(?:Appearances?\b|For the\b|O\s*R\s*D\s*E\s*R\b|Per\s*:)', block, maxsplit=1, flags=re.I | re.M)[0]
    entries = []
    for line in block.splitlines():
        prefixes = list(re.finditer(r'\b(?:Shri\.?|Sh\.|Justice)\s+', line, re.I))
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
            profile = re.search(r'(?:The )?Resolution Applicant is\s+(?:an?\s+)?([^.;]{5,200})[.;]', flat, re.I)
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
