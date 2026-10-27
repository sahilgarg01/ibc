from backend.extraction import READER_VERSION, needs_pdf_reread


def test_failed_tables_are_retried_even_with_current_reader_version():
    extraction = {'reader_version': READER_VERSION, 'pages': [
        {'text': 'Readable PDF text ' * 10, 'layout_text': 'Table heading'}
    ]}
    assert needs_pdf_reread(extraction)
    extraction['pages'][0]['tables'] = []
    assert not needs_pdf_reread(extraction)  # A successfully read page may have no tables.


def test_failed_ocr_is_retried_but_successful_ocr_is_cached():
    extraction = {'reader_version': READER_VERSION, 'pages': [
        {'text': '', 'layout_text': '', 'tables': [], 'ocr_attempted': True}
    ], 'warnings': ['OCR could not process the scanned pages. Check OCR dependencies and retry extraction.']}
    assert needs_pdf_reread(extraction)
    extraction['warnings'] = []
    assert not needs_pdf_reread(extraction)
