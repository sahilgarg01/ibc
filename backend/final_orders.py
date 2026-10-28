"""Conservative screening of IBBI listings and operative PDF order text."""
import re

SCREENING_VERSION = 'final-screen-v2'


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
    r'\b(?:this\s+adjudicating\s+authority|this\s+tribunal|we)\s+(?:hereby\s+)?'
    r'(?:orders?|directs?)\s+(?:the\s+)?liquidation\s+of\s+the\s+corporate\s+debtor\b|'
    r'\bthe\s+corporate\s+debtor\s+is\s+(?:hereby\s+)?ordered\s+to\s+be\s+liquidated\b|'
    r'\bthe\s+corporate\s+debtor\b.{0,140}?\bis\s*,?\s*'
    r'(?:accordingly\s*,?\s*)?(?:hereby\s+)?(?:directed|ordered)\s+to\s+be\s+liquidated\b|'
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
    first_page = ' '.join((pages[0].get('text') or '').split())
    opening = ' '.join(' '.join((page.get('text') or '').split()) for page in pages[:2])[:10000]
    has_ocr = any(page.get('ocr_text') or page.get('extraction_method') == 'ocr' for page in pages[:2])
    def heading(pattern):
        return pattern.replace(r'\s+', r'\s*') if has_ocr else pattern
    if re.search(heading(r'\bIN\s+THE\s+HIGH\s+COURT(?:\s+OF\s+[A-Z]+)?\b|\bIN\s+THE\s+SUPREME\s+COURT(?:\s+OF\s+INDIA)?\b|'
                 r'\bIN\s+THE\s+NATIONAL\s+COMPANY\s+LAW\s+APPELLATE\s+TRIBUNAL\b'), first_page[:1200], re.I):
        return None
    if not re.search(heading(r'\b(?:IN\s+THE\s+)?NATIONAL\s+COMPANY\s+LAW\s+TRIBUNAL\b'), opening, re.I):
        return None
    # The operative paragraph can precede several pages of directions or
    # annexures; a last-quarter-only search missed real liquidation orders.
    tail = pages[-max(3, (len(pages) + 1) // 2):]
    found = []
    for page in tail:
        flat = ' '.join((page.get('text') or '').split())
        for outcome, pattern in (('resolution_approved', RESOLUTION), ('liquidation_ordered', LIQUIDATION)):
            if page.get('ocr_text') or page.get('extraction_method') == 'ocr':
                pattern = re.compile(pattern.pattern.replace(r'\s+', r'\s*'), re.I)
            for match in pattern.finditer(flat):
                found.append({'outcome':outcome,'page':page['page'],'excerpt':match.group(0)})
    if not found or len({item['outcome'] for item in found}) != 1:
        return None
    return found[-1]
