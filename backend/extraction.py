import re
from io import BytesIO

from pypdf import PdfReader
from .ocr import read_scanned_pages

READER_VERSION = 'shared-tables-3'


def has_substantial_image(page):
    """Find image tables even when the page has an embedded text header."""
    def inspect(resources, depth=0):
        if depth > 5 or not resources:
            return False
        resources = resources.get_object()
        xobjects = resources.get('/XObject')
        if not xobjects:
            return False
        for reference in xobjects.get_object().values():
            obj = reference.get_object()
            if obj.get('/Subtype') == '/Image' and obj.get('/Width', 0) >= 300 and obj.get('/Height', 0) >= 120:
                return True
            if obj.get('/Subtype') == '/Form' and inspect(obj.get('/Resources'), depth + 1):
                return True
        return False
    return inspect(page.get('/Resources'))


def read_tables(content):
    import pdfplumber
    results = {}
    with pdfplumber.open(BytesIO(content)) as document:
        for number, page in enumerate(document.pages, 1):
            try:
                tables = page.extract_tables()
                if not tables:
                    text = page.extract_text() or ''
                    if re.search(r'Fair\s+Value|Amount\s+Claimed|Claimed[\s\S]{0,100}Admitted', text, re.I):
                        tables = page.extract_tables({'vertical_strategy': 'text', 'horizontal_strategy': 'text',
                            'min_words_vertical': 2, 'text_x_tolerance': 2})
                results[number] = tables
            except Exception:
                results[number] = []
            finally:
                page.close()
    return results


def read_alternate_pages(content, page_numbers):
    """Read embedded text with an independent PDF engine before using OCR."""
    import pypdfium2 as pdfium
    results = {}
    document = pdfium.PdfDocument(content)
    try:
        for number in page_numbers:
            page = document[number - 1]
            try:
                textpage = page.get_textpage()
                try:
                    results[number] = textpage.get_text_bounded().replace('\r\n', '\n')
                finally:
                    textpage.close()
            except Exception:
                # One unsupported page must not discard other recovered pages.
                continue
            finally:
                page.close()
    finally:
        document.close()
    return results


def pdf_subject(pages, metadata=None):
    metadata = metadata or {}
    for key in ("/Subject", "/Title"):
        value = " ".join(str(metadata.get(key) or "").split())
        if 5 <= len(value) <= 500 and value.casefold() not in {"untitled", "document", "microsoft word"}:
            return value, "pdf_metadata"
    for page in pages[:3]:
        lines = [" ".join(line.split()) for line in page["text"].splitlines() if line.strip()]
        for i, line in enumerate(lines):
            match = re.match(r"(?:subject|re)\s*[:\-]\s*(.+)", line, re.I)
            if match and len(match.group(1)) > 4:
                return match.group(1)[:500], "text_subject"
            if re.search(r"in the matter of", line, re.I):
                heading = " ".join(lines[i:i+4])
                return heading[:500], "text_heading"
        if lines:
            return " ".join(lines[:5])[:300], "text_heading"
    return None, "unavailable"


def extract_pdf(content: bytes) -> dict:
    if not content.startswith(b"%PDF-"):
        raise ValueError("Upload a valid PDF file")
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("Password-protected PDFs are not supported")
        if not 1 <= len(reader.pages) <= 250:
            raise ValueError("PDF must contain between 1 and 250 pages")
        pages = []
        candidates = []
        for number, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            try:
                layout = page.extract_text(extraction_mode='layout') or text
            except Exception:
                layout = text  # Text extraction still works when layout reconstruction fails.
            pages.append({"page": number, "text": text, "layout_text": layout, "extraction_method":"pdf_text"})
            try:
                pages[-1]['has_image_content'] = has_substantial_image(page)
            except Exception:
                pages[-1]['has_image_content'] = False
            # Only conservative identifiers, never inferred financial values.
            for match in re.finditer(r"\b[LU]\d{5}[A-Z]{2}\d{4}(?:PLC|PTC)\d{6}\b", text):
                candidates.append({"field": "cin", "value": match.group(), "page": number})
        warnings = []
        try:
            tables = read_tables(content)
            for p in pages:
                p['tables'] = tables.get(p['page'], [])
        except Exception:
            warnings.append('Structured table extraction failed; table fields require review.')
        unreadable = [p['page'] for p in pages if len(p['text'].strip()) < 30]
        if unreadable:
            try:
                alternate = read_alternate_pages(content, unreadable)
                for p in pages:
                    recovered = alternate.get(p['page'], '')
                    if len(recovered.strip()) > len(p['text'].strip()):
                        p.update(text=recovered, layout_text=recovered, extraction_method='pdfium_text')
            except Exception:
                warnings.append('Alternate PDF reader failed; unreadable pages will use OCR.')
        scanned=[p['page'] for p in pages if len(p['text'].strip())<30 or p.get('has_image_content')]
        if scanned:
            try:
                recognized=read_scanned_pages(content,scanned)
                for p in pages:
                    if p['page'] in recognized:
                        result=recognized[p['page']]
                        p['ocr_attempted']=True
                        p['ocr_confidence']=result['confidence']
                        p['ocr_text'] = result['text']
                        p['ocr_layout_text'] = result.get('layout_text', result['text'])
                        if len(result['text'].strip())>len(p['text'].strip()):
                            p['embedded_text'] = p['text']
                            p['embedded_layout_text'] = p['layout_text']
                            p['text']=result['text'];p['layout_text']=result.get('layout_text', result['text']);p['extraction_method']='ocr'
                if any(p.get('extraction_method')=='ocr' for p in pages):
                    warnings.append('Text on scanned pages was recognized with OCR. Verify names, dates and amounts against the PDF.')
            except Exception:
                for p in pages:
                    if p['page'] in scanned: p['ocr_attempted']=True
                warnings.append('OCR could not process the scanned pages. Check OCR dependencies and retry extraction.')
        if any(len(p["text"].strip()) < 30 for p in pages):
            warnings.append("Some pages still have little or no readable text after extraction. Review the original PDF.")
        candidates=[]
        for p in pages:
            for match in re.finditer(r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b",p['text']):
                candidates.append({'field':'cin','value':match.group(),'page':p['page']})
        subject, subject_source = pdf_subject(pages, reader.metadata)
        return {"reader_version": READER_VERSION, "pages": pages, "candidates": candidates, "warnings": warnings,
                "subject": subject, "subject_source": subject_source}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The PDF could not be read; it may be damaged or unsupported") from exc
