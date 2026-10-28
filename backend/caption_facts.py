"""Explicit caption and narrative variants used by tribunal orders."""
import re
from .field_rules import clean_entity


def enrich_captions(extraction, put, money, iso_date):
    company = r'([A-Z][A-Za-z0-9 &.,-]{2,120}?(?:Private Limited|Pvt\.?\s*Ltd\.?|Limited|Ltd\.?))'
    person = r'(?:CS\.?|CA\.?|Mr\.?|Ms\.?|Mrs\.?|Dr\.?)\s+(?:\(Dr\.?\)\s*)?([A-Z][A-Za-z.-]*(?:\s+[A-Z][A-Za-z.-]*){1,5}?)'
    def referenced_liquidator(value):
        # A PDF's positioning can split a word in an appointment. Accept a
        # cleaner repeated name only when every letter agrees with that name.
        key = re.sub(r'[^a-z]', '', value.lower())
        for source in extraction['pages']:
            for match in re.finditer(r'Liquidator,\s*' + person + r'\s*\.', ' '.join(source['text'].split()), re.I):
                candidate = match[1].rstrip('.')
                if re.sub(r'[^a-z]', '', candidate.lower()) == key:
                    return candidate
        return value
    caption_applicant = None
    for page in extraction['pages'][:3]:
        caption = re.search(r'(?m)^[ \t]*' + person +
            r'[ ,\t]*\n[ \t.…]*(?:Resolution\s+Professional\s*/\s*Applicant|Applicant\s*/\s*Resolution\s+Professional)\b', page['text'], re.I)
        if caption:
            caption_applicant = ' '.join(caption[1].split()).rstrip('.,')
            put('professional', caption_applicant, page['page'], caption.group())
            break
    for page in extraction['pages']:
        n, text = page['page'], page['text']
        flat = ' '.join(text.split())
        def capture(field, pattern, transform=lambda value: value, source=flat):
            match = re.search(pattern, source, re.I | re.M)
            if match:
                put(field, transform(match.group(1)), n, match.group(0))
        if n <= 3:
            capture('case_number', r'\b(C\.?\s*P\.?\s*\(\s*IB\s*\)\s*/\s*\d+\s*/\s*\([A-Z-]+\)\s*/\s*\d{4})\b')
            capture('tribunal', r'NATIONAL\s+COMPANY\s+LAW\s+TRIBUNAL\s+((?!DIVISION\b)[A-Z]+\s+BENCH)\b', lambda v: 'NCLT ' + v.split()[0].title())
            capture('case_number', r'\b(C\.?\s*P\.?\s*\(\s*IB\s*\)\s*/\s*\d+(?:/\d+)?/[A-Z]+/\d{4})\b')
            role_caption = re.search(r'\b(?:Versus|V/s\.?|Vs\.?)\s+(?P<name>(?:M/s\.?\s*)?[A-Z][A-Za-z0-9 &.,()\-\n]{2,150}?(?:Private\s+Limited|Pvt\.?\s*Ltd\.?|Limited|Ltd\.?))(?P<details>.{0,600}?)[.…\s]*(?:Respondent\s*/\s*)?Corporate\s+Debtor\b', text, re.I | re.S)
            if role_caption:
                put('company', clean_entity(role_caption['name']), n, role_caption.group())
                address = re.search(r'Having\s+its\s+registered\s+(?:address|office)\s+at\s*:\s*(.+?)(?=[.…]*\s*(?:Respondent|Corporate Debtor)|$)', role_caption['details'], re.I | re.S)
                if address:
                    put('registered_office', address.group(1).strip(' .'), n, role_caption.group())
            # Identify the debtor by the caption's role, rather than selecting
            # the first company or address (often the petitioning bank).
            debtor = re.search(r'(?m)^[ \t]*(?P<name>(?:M/s\.?\s*)?[A-Z][A-Za-z0-9 &.,()\-\n]{2,150}?(?:Private\s+Limited|Pvt\.?\s*Ltd\.?|Limited|Ltd\.?))\s*,?\s*\(\s*CIN\s*:\s*(?P<cin>[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\s*\)(?P<details>.{0,600}?)[.…\s]*Corporate\s+Debtor\b', text, re.I | re.S)
            if debtor:
                name = re.split(r'\b(?:Versus|V/s\.?|Vs\.?)\s+', debtor['name'], flags=re.I)[-1]
                put('company', clean_entity(name), n, debtor.group())
                put('cin', debtor['cin'].upper(), n, debtor.group())
                address = re.search(r'having\s+its\s+registered\s+office\s+at\s+(.+?)(?:;|$)', debtor['details'], re.I | re.S)
                if address:
                    put('registered_office', address.group(1).strip(' .'), n, debtor.group())
            capture('professional', r'^\s*([A-Z][A-Za-z.-]+(?:\s+[A-Z][A-Za-z.-]+){1,4}),\s*(?:the\s+)?Resolution\s+Professional\s+of\b', source=text)
            capture('tribunal', r'NATIONAL\s+COMPANY\s+LAW\s+TRIBUNAL\s+(COURT\s*[-–—]\s*[IVX]+\s*,\s*[A-Z]+\s+BENCH|[A-Z]+\s+BENCH\s*,\s*[IVX]+)', lambda v: 'NCLT ' + v)
            capture('professional', person + r'\s*(?:,\s*[“"(]*Applicant[”")]*\s*)?\s*(?:[“"(]*Applicant[”")]*\s*)?Resolution Professional\s+(?:of|for)\b')
            capture('case_number', r'\b(CP\s*\(\s*IB\s*C?\s*\)\s*/\s*\d+\s*\([A-Z]+\)\s*/\s*\d{4})')
            capture('tribunal', r'NATIONAL COMPANY LAW TRIBUNAL\s*,?\s*(DIVISION BENCH\s*(?:\(COURT\s*[–—-]\s*[IVX]+\)|[–—-]\s*[IVX]+)\s*,?\s*[A-Z]+)', lambda v: 'NCLT ' + v)
            capture('order_date', r'^\s*Date\s*:\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})', iso_date, text)
            capture('professional', r'^\s*([A-Z][A-Za-z .-]{3,90})\s*\n\s*Resolution Professional (?:for|of)\b', source=text)
        capture('applicant', r'Successful Resolution Applicant\s*(?:\(SRA\)\s*)?(?:viz\.?\s*,?\s*|is\s+)' + company)
        capture('applicant', person + r'\s*\(["“\s]*(?:Successful\s+)?Resolution\s+Applicant["”\s]+or\s+["“\s]*RA["”\s]*\)')
        capture('registered_office', r'The Corporate Debtor is\b.{0,200}?having its registered office at\s+(.{5,400}?)(?=\.\s+The Corporate Debtor|$)')
        capture('registered_office', r'Resolution\s+Professional\s+of\s+(?:M/s\.?\s*)?' + company[1:-1] + r'\s+Having\s+its\s+registered\s+(?:address|office)\s+at\s*:\s*(.{5,350}?\b\d{3}\s*\d{3}\b)')
        capture('industry', r'The Corporate Debtor was involved in\s+([^.;]{5,150})')
        capture('industry', r'The Corporate Debtor (?:is|was)\s+(?:primarily\s+)?engaged in\s+(?:the\s+)?(?:business\s+of\s+)?(.{5,180}?)(?:\.\s|$)', lambda v: v.rstrip('.'))
        capture('coc_voting_share', r'sole\s+Financial\s+Creditor\s+holding\s+(\d{1,3}(?:\.\d+)?\s*%)\s+of\s+the\s+voting\s+share', lambda v: v.replace(' ', ''))
        capture('coc_voting_share', r'CoC\s+has\s+passed\s+a\s+resol\s*ution\s+in\s+favour\s+of\s+Liquidation\s+with\s+(\d{1,3}(?:\.\d+)?\s*%)\s+majority', lambda v: v.replace(' ', ''))
        capture('order_date', r'Date\s+of\s+pronouncement\s*:\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})', iso_date)
        capture('admission_date', r'This Tribunal\s+vide\s+its\s+order\s+dated\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})\s+admitted\s+the\s+application\b.{0,150}?for\s+initiation\s+of\s+Corporate\s+Insolvency', iso_date)
        capture('admission_date', r'Corporate Insolvency Resolution\s*Process\s*(?:\([^)]{0,70}\)\s*)?of\s+(?:M/s\.?\s*)?' + company[1:-1] + r'\s*(?:\([^)]{0,70}\)\s*)?commenced\s+vide\s+order\s+dated\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})', iso_date)
        capture('admission_date', r'The Petition was admitted by this Tribunal vide an order dated\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})', iso_date)
        capture('admission_date', r'Corporate Insolvency Resolution\s*process\s*(?:\([“"\s]*CIRP[”"\s]*\)\s*)?of\s+(?:the\s+)?(?:M/s\.?\s*)?' + company[1:-1] + r'\s+was\s+initiated\s+by\s+this\s+Tribunal\s+vide\s+Order\s+dated\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})', iso_date)
        capture('irp', person + r'\s+was appointed as\s+(?:the\s+)?Interim Resolution Professional\b')
        if caption_applicant:
            appointment = re.search(r'\bthe\s+Applicant\s+was\s+appointed\s+as\s+(?:the\s+)?Interim\s+Resolution\s+Professional\b', flat, re.I)
            if appointment:
                put('irp', caption_applicant, n, appointment.group())
        capture('irp', r'appointed\s+' + person + r'\s+as\s+(?:the\s+)?Interim\s+Resolution\s+Professional\b')
        # Use an actual appointment, not a prayer or a proposal for appointment.
        capture('liquidator', person + r'\s+is appointed\s+a\s*s\s+the\s+Liquidator\b')
        capture('liquidator', r'we\s+hereby\s+appoint\s+' + person + r'\s+having\b.{0,400}?\bas\s+the\s+Liquidator\b')
        capture('liquidator', r'we\s+hereby\s+appoint\s+' + person + r'\s*,\s*Insolvency\s+Professional\s*,\s*having\b.{0,650}?\bas\s+(?:the\s+)?Liquidator\s+of\s+the\s+Corporate\s+Debtor\b', referenced_liquidator)
        capture('liquidator', person + r',?\s+having\s+IBBI\s+Registration\s+No\..{0,120}?is\s+hereby\s+appointed\s+as\s+the\s+Liquidator\b')
        capture('liquidator', r'Accordingly,\s+' + person + r'\s+bearing\s+Registration\s+No\..{0,550}?to\s+act\s+as\s+the\s+Liquidator\b')
        capture('liquidator', r'we\s+hereby\s+appoint\s+' + person + r'\s+bearing\s+Registration\s+No\..{0,1000}?to\s+act\s+as\s+the\s+Liquidator\b')
        debtor_cin = re.search(r'Corporate\s+Debtor,\s*' + company[1:-1] + r'\s*,\s*CIN\s*:?\s*([LU](?:\s*[A-Z0-9]){20})\s*,', flat, re.I)
        if debtor_cin:
            cin = re.sub(r'\s+', '', debtor_cin.group(1)).upper()
            if re.fullmatch(r'[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}', cin):
                put('cin', cin, n, debtor_cin.group())
        capture('irp', r'\band\s+([A-Z][A-Za-z .-]{3,80}?)\s*\(erstwhile IRP\)\s*was appointed as the Interim Resolution Professional')
        for field, label in [('fair_value', 'Fair Value'), ('liquidation_value', 'Liquidation Value')]:
            capture(field, label + r'(?: of the Corporate Debtor)?\s+is\s+(?:INR|Rs\.?)\s*([\d,]+(?:\.\d+)?)', money)
        capture('applicant_location', r'Successful Resolution Applicant\s*\(SRA\)\s*is\s+' + company[1:-1] + r',\s*having its registered office at\s+(.{5,250}?\b\d{6})')
