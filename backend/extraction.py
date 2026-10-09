import re
from io import BytesIO

from pypdf import PdfReader
from .ocr import read_scanned_pages


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
            text = page.extract_text() or ""
            try:
                layout = page.extract_text(extraction_mode='layout') or text
            except Exception:
                layout = text  # Text extraction still works when layout reconstruction fails.
            pages.append({"page": number, "text": text, "layout_text": layout, "extraction_method":"pdf_text"})
            # Only conservative identifiers, never inferred financial values.
            for match in re.finditer(r"\b[LU]\d{5}[A-Z]{2}\d{4}(?:PLC|PTC)\d{6}\b", text):
                candidates.append({"field": "cin", "value": match.group(), "page": number})
        warnings = []
        scanned=[p['page'] for p in pages if len(p['text'].strip())<30]
        if scanned:
            try:
                recognized=read_scanned_pages(content,scanned)
                for p in pages:
                    p['extraction_method']='pdf_text'
                    if p['page'] in recognized:
                        result=recognized[p['page']]
                        p['ocr_attempted']=True
                        p['ocr_confidence']=result['confidence']
                        if len(result['text'].strip())>len(p['text'].strip()):
                            p['text']=result['text'];p['layout_text']=result['text'];p['extraction_method']='ocr'
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
        return {"pages": pages, "candidates": candidates, "warnings": warnings,
                "subject": subject, "subject_source": subject_source}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The PDF could not be read; it may be damaged or unsupported") from exc
