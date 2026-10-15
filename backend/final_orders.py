"""Conservative screening of IBBI listings and operative PDF order text."""
import re


NONFINAL_LISTING = re.compile(
    r'\b(?:interlocutory|interim application|withdrawal|withdrawn|appointment|replacement|'
    r'extension|exclusion of time|admission of (?:application|petition)|restoration|'
    r'impleadment|condonation|dismissal|stay of proceedings|moratorium)\b', re.I
)
FINAL_LISTING = re.compile(
    r'\b(?:approval of (?:the )?resolution plan|resolution plan approved|'
    r'liquidation (?:ordered|order)|order for liquidation)\b', re.I
)
RESOLUTION = re.compile(
    r'\b(?:the|this)\s+resolution\s+plan(?:\s+annexed\s+to\s+the\s+application)?'
    r'\s+is\s+(?:hereby\s+)?approved\b|'
    r'\bwe\s+(?:hereby\s+)?approve\s+the\s+resolution\s+plan\b', re.I
)
LIQUIDATION = re.compile(
    r'\bwe\s+(?:hereby\s+)?order\s+liquidation\s+of\s+the\s+corporate\s+debtor\b|'
    r'\bthe\s+corporate\s+debtor\s+is\s+(?:hereby\s+)?ordered\s+to\s+be\s+liquidated\b|'
    r'\bliquidation\s+of\s+the\s+corporate\s+debtor\s+is\s+(?:hereby\s+)?ordered\b', re.I
)


def skip_listing(subject, remarks):
    """Avoid downloading unmistakably procedural orders; ambiguous rows need PDF review."""
    description = f'{subject or ""} {remarks or ""}'
    return bool(NONFINAL_LISTING.search(description) and not FINAL_LISTING.search(description))


def final_decision(extraction):
    """Return an operative outcome with its PDF page, or None when unproven."""
    pages = extraction.get('pages') or []
    if not pages:
        return None
    # Other courts often quote an earlier NCLT plan approval while deciding a
    # different dispute. Their quoted language is not this PDF's final order.
    opening = ' '.join((pages[0].get('text') or '').split())[:1200]
    if re.search(r'\bIN\s+THE\s+HIGH\s+COURT\b|\bIN\s+THE\s+SUPREME\s+COURT\b|'
                 r'\bIN\s+THE\s+NATIONAL\s+COMPANY\s+LAW\s+APPELLATE\s+TRIBUNAL\b', opening, re.I):
        return None
    if not re.search(r'\b(?:IN\s+THE\s+)?NATIONAL\s+COMPANY\s+LAW\s+TRIBUNAL\b', opening, re.I):
        return None
    tail = pages[-max(3, (len(pages) + 3) // 4):]
    found = []
    for page in tail:
        flat = ' '.join((page.get('text') or '').split())
        for outcome, pattern in (('resolution_approved', RESOLUTION), ('liquidation_ordered', LIQUIDATION)):
            for match in pattern.finditer(flat):
                found.append({'outcome':outcome,'page':page['page'],'excerpt':match.group(0)})
    if not found or len({item['outcome'] for item in found}) != 1:
        return None
    return found[-1]
