"""Explicit source units, shared by valuation and creditor table readers."""
import re
from decimal import Decimal


UNIT_FACTORS = {'INR': Decimal(1), 'crore': Decimal(10000000),
                'lakh': Decimal(100000), 'million': Decimal(1000000)}
UNIT_TOKEN = r'(crores?|cr\.?|lakhs?|lacs?|millions?|rupees?|inr|rs)\b'


def normalize_unit(value):
    value = value.lower().rstrip('.')
    return 'crore' if value.startswith('cr') else 'lakh' if value.startswith(('lakh', 'lac')) else 'million' if value.startswith('million') else 'INR'


def value_unit(value, default='INR'):
    # A currency prefix alone does not cancel a table's explicit scale.
    match = re.search(r'\d\s*' + UNIT_TOKEN, value or '', re.I)
    return normalize_unit(match.group(1)) if match else default


def declared_units(text):
    pattern = r'(?:\bin\s+(?:(?:Rs\.?|INR|rupees)\s*)?|\b(?:amounts?|values?)\s*\(\s*(?:Rs\.?|INR)?\s*)' + UNIT_TOKEN
    return [(match.start(), normalize_unit(match.group(1))) for match in re.finditer(pattern, text or '', re.I)]


def table_unit(page, table, previous_page=None):
    header = ' '.join(str(cell or '') for row in table[:2] for cell in row)
    units = declared_units(header)
    if units:
        return units[-1][1]
    text = ' '.join(page.get('embedded_text', page.get('text', '')).split())
    # Use the nearest declaration before this table, never a later unrelated value.
    first_row = table[0] if table else []
    full_heading = ' '.join(' '.join(str(cell).split()) for cell in first_row if cell)
    position = text.casefold().find(full_heading.casefold()) if full_heading else -1
    if position < 0:
        anchor = next((' '.join(str(cell).split()) for cell in first_row
                       if cell and len(str(cell).strip()) > 4), '')
        position = text.casefold().rfind(anchor.casefold()) if anchor else -1
    prefix = text[:position] if position >= 0 else text
    units = declared_units(prefix)
    if units:
        return units[-1][1]
    if previous_page:
        units = declared_units(' '.join(previous_page.get('text', '').split())[-600:])
        if units:
            return units[-1][1]
    return 'INR'
