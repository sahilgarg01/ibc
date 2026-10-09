from io import BytesIO
from pypdf import PdfWriter
from backend.extraction import extract_pdf


def blank_pdf():
    writer=PdfWriter();writer.add_blank_page(width=600,height=800)
    stream=BytesIO();writer.write(stream)
    return stream.getvalue()


def test_scanned_page_ocr_fallback(monkeypatch):
    monkeypatch.setattr('backend.extraction.read_scanned_pages',lambda content,pages:{1:{'text':'Subject: Example scanned court order with readable text','confidence':.94}})
    result=extract_pdf(blank_pdf())
    assert result['pages'][0]['text'].startswith('Subject:')
    assert result['pages'][0]['extraction_method']=='ocr'
    assert result['pages'][0]['ocr_confidence']==.94
    assert result['subject']=='Example scanned court order with readable text'


def test_ocr_failure_preserves_pdf_extraction(monkeypatch):
    def failed(*args): raise RuntimeError('OCR failure')
    monkeypatch.setattr('backend.extraction.read_scanned_pages',failed)
    result=extract_pdf(blank_pdf())
    assert result['pages'][0]['text']==''
    assert result['pages'][0]['ocr_attempted']
    assert any('OCR could not process' in warning for warning in result['warnings'])
