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


def test_one_failed_scan_preserves_other_pages_and_marks_the_failure(monkeypatch):
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=600, height=800)
    stream = BytesIO()
    writer.write(stream)
    monkeypatch.setattr('backend.extraction.read_alternate_pages', lambda *args: {})
    monkeypatch.setattr('backend.extraction.read_scanned_pages', lambda *args: {
        1: {'text': 'Corporate debtor: Future Manufacturing Limited', 'confidence': .98},
        2: {'error': 'OCR could not process this page.'},
        3: {'text': 'Resolution professional: Mira Shah', 'confidence': .97},
    })
    result = extract_pdf(stream.getvalue())
    from backend.case_extraction import extract_case
    from backend.extraction import needs_pdf_reread
    record, report = extract_case(result, 'future-scan')
    assert record['company']['value'] == 'Future Manufacturing Limited'
    assert record['professional']['value'] == 'Mira Shah'
    assert record['professional']['page'] == 3
    assert result['pages'][1]['ocr_error']
    assert all(page['ocr_attempted'] for page in result['pages'])
    assert any('scanned pages: 2.' in warning for warning in result['warnings'])
    assert needs_pdf_reread(result)


def test_ocr_engine_continues_after_a_broken_page_and_closes_resources(monkeypatch):
    import sys
    from types import SimpleNamespace
    from backend import ocr
    closed = []
    class Page:
        def __init__(self, number): self.number = number
        def get_size(self): return (600, 800)
        def render(self, scale):
            return SimpleNamespace(to_numpy=lambda: self.number,
                                   close=lambda: closed.append(('bitmap', self.number)))
        def close(self): closed.append(('page', self.number))
    class Document:
        def __getitem__(self, index):
            if index == 1: raise RuntimeError('Damaged page')
            return Page(index + 1)
        def close(self): closed.append(('document', None))
    monkeypatch.setitem(sys.modules, 'pypdfium2', SimpleNamespace(PdfDocument=lambda _: Document()))
    monkeypatch.setitem(sys.modules, 'rapidocr_onnxruntime', SimpleNamespace(RapidOCR=lambda **kwargs: None))
    box = [[0, 0], [200, 0], [200, 20], [0, 20]]
    monkeypatch.setattr(ocr, '_engine', lambda number: ([[box, 'page '+str(number), .99]], None))
    result = ocr.read_scanned_pages(b'test', [1, 2, 3])
    assert result[1]['text'] == 'page 1'
    assert result[2]['error']
    assert result[3]['text'] == 'page 3'
    assert closed == [('bitmap', 1), ('page', 1), ('bitmap', 3), ('page', 3), ('document', None)]


def test_real_scan_transcript_populates_every_explicit_draft_field():
    import json
    from pathlib import Path
    from backend.case_extraction import extract_case
    from backend.models import CaseRecord
    extraction = json.loads((Path(__file__).parent/'fixtures/future-scan-ocr.json').read_text())
    original = json.dumps(extraction)
    record, report = extract_case(extraction, 'future-scan')
    assert json.dumps(extraction) == original
    assert not report['missing_fields']
    assert set(report['field_review']) == set(CaseRecord.model_fields) - {'notes'}
    assert all(item['status'] == 'extracted' for item in report['field_review'].values())
    assert record['cin']['value'] == 'U12345MH2010PTC123456'
    assert record['applicant_cin']['value'] == 'U54321MH2012PLC654321'
    assert record['admission_date']['value'] == '2025-02-01'
    assert record['resolution_date']['value'] == '2026-03-02'
    assert record['applicant_incorporated']['value'] == '2012-06-05'
    assert record['fair_value']['value'] == '100000000.00'
    assert record['liquidation_value']['value'] == '80000000.00'
    assert record['claims'][0]['plan_amount'] == '60000000.00'
    assert record['claims'][0]['actual_paid'] == '10000000.00'
    assert record['plan_payments'][0]['amount'] == '60000000.00'
    assert record['plan_payments'][0]['page'] == 3  # Explicit creditor allocation, not guessed merged table digits.
    assert record['outcome'] == 'resolution_approved'
    assert record['outcome_evidence']['page'] == 4
    # OCR names stay faithful to the recognized text; no arbitrary word splits.
    assert record['company']['value'] == 'FutureScannedIndustriesLimited'


def test_compact_ocr_court_heading_does_not_turn_quoted_approval_into_outcome():
    from backend.case_extraction import extract_case
    text = ('INTHESUPREMECOURTOFINDIA\n'
            'TheNATIONALCOMPANYLAWTRIBUNALpreviouslysaid:\n'
            'TheResolutionPlanisherebyapproved.')
    record, _ = extract_case({'pages': [{'page': 1, 'text': text, 'ocr_text': text}]}, 'quote')
    assert record['outcome'] == 'unknown'


def test_proposed_and_actual_labeled_payments_are_kept_separate():
    from backend.case_extraction import extract_case
    text = 'Creditor: Bank A; Type: securedfinancial; Claimed: Rs 100; Admitted: Rs 90; Planamount: Rs 50; Actualpaid: Rs 20'
    record, _ = extract_case({'pages': [{'page': 1, 'text': text}]}, 'allocation')
    assert record['plan_payments'][0]['amount'] == '50.00'
    assert record['claims'][0]['actual_paid'] == '20.00'
    text = 'Creditor: Bank A; Type: securedfinancial; Claimed: Rs 100; Admitted: Rs 90; Actualpaid: Rs 20'
    record, _ = extract_case({'pages': [{'page': 1, 'text': text}]}, 'actual-only')
    assert not record['plan_payments']
