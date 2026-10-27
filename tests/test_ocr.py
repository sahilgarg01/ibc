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


def test_alternate_reader_recovers_before_ocr(monkeypatch):
    text = 'Corporate debtor: Example Industries Private Limited\nDebtor CIN: U12345MH2020PTC123456'
    monkeypatch.setattr('backend.extraction.read_alternate_pages', lambda *args: {1: text})
    def unexpected(*args):
        raise AssertionError('Readable alternate text must not need OCR')
    monkeypatch.setattr('backend.extraction.read_scanned_pages', unexpected)
    result = extract_pdf(blank_pdf())
    assert result['pages'][0]['extraction_method'] == 'pdfium_text'
    assert result['candidates'][0]['value'] == 'U12345MH2020PTC123456'
    assert not result['warnings']
    from backend.case_extraction import extract_case
    record, report = extract_case(result, 'test-document')
    assert record['company']['value'] == 'Example Industries Private Limited'
    assert 'company' in report['populated_fields']
    assert 'claims' in report['missing_fields']


def test_ocr_layout_preserves_table_columns():
    from backend.ocr import layout_rows
    rows = [([[200, 11], [240, 11], [240, 21], [200, 21]], '1000', .9),
            ([[0, 10], [80, 10], [80, 20], [0, 20]], 'Creditor', .9)]
    assert layout_rows(rows).startswith('Creditor  ')
    assert len(layout_rows(rows).splitlines()) == 1
    assert layout_rows(rows).endswith('1000')


def test_image_table_is_ocr_processed_even_with_readable_header(monkeypatch):
    header = 'NATIONAL COMPANY LAW TRIBUNAL MUMBAI BENCH - page header'
    monkeypatch.setattr('backend.extraction.read_alternate_pages', lambda *args: {1: header})
    monkeypatch.setattr('backend.extraction.has_substantial_image', lambda page: True)
    called = []
    def recognize(content, pages):
        called.extend(pages)
        return {1: {'text': header + '\nFinancial Proposal: Total INR 19,00,00,000', 'confidence': .96}}
    monkeypatch.setattr('backend.extraction.read_scanned_pages', recognize)
    result = extract_pdf(blank_pdf())
    assert called == [1]
    assert result['pages'][0]['extraction_method'] == 'ocr'
    assert result['pages'][0]['embedded_text'] == header
    assert '19,00,00,000' in result['pages'][0]['text']
