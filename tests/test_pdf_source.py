from io import BytesIO
import json

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from backend.db import connection
from backend.main import ROOT, create_app
from backend import pdf_source


def test_source_pages_come_from_python_and_are_cached_without_changing_fields(tmp_path, monkeypatch):
    pdf = (ROOT/'samples/indo-global-resolution-order.pdf').read_bytes()
    with TestClient(create_app(tmp_path)) as client:
        ids = client.post('/api/documents', files={'file': ('source.pdf', pdf)}).json()
        before = client.get(f'/api/cases/{ids["case_id"]}').json()
        # The fixture's model page text is unrelated to the actual PDF text.
        model_pages = before['documents'][0]['extraction']['pages']
        monkeypatch.setattr('backend.main.extract_pdf', lambda _: (_ for _ in ()).throw(AssertionError('Codex must not run')))
        result = client.get(f'/api/documents/{ids["document_id"]}/content')
        assert result.status_code == 200, result.text
        source = result.json()
        assert source['source_provider'] == 'python'
        assert len(source['pages']) == 23
        assert all(page['extraction_method'] == 'python-pypdf' for page in source['pages'])
        assert source['pages'] != model_pages
        assert 'Indo Global' in source['pages'][0]['text']
        assert 'Secured' in source['pages'][8]['text']
        assert not any('Codex' in warning for warning in source['warnings'])
        # Subsequent views read the Python cache, never the model response.
        monkeypatch.setattr('backend.main.read_source', lambda _: (_ for _ in ()).throw(AssertionError('Cache should be reused')))
        assert client.get(f'/api/documents/{ids["document_id"]}/content').json()['pages'] == source['pages']
        after = client.get(f'/api/cases/{ids["case_id"]}').json()
        assert after == before
        with connection(tmp_path, database_url='') as db:
            saved = db.execute('SELECT content FROM document_source_evidence WHERE document_id=?', (ids['document_id'],)).fetchone()
            assert json.loads(saved['content'])['provider'] == 'python'


def test_image_fallback_renders_original_blank_page(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    stream = BytesIO()
    writer.write(stream)
    with TestClient(create_app(tmp_path)) as client:
        ids = client.post('/api/documents', files={'file': ('blank.pdf', stream.getvalue())}).json()
        source = client.get(f'/api/documents/{ids["document_id"]}/content').json()
        assert len(source['pages']) == 1
        assert source['pages'][0]['text'] == ''
        image = client.get(f'/api/documents/{ids["document_id"]}/pages/1/image')
        assert image.status_code == 200, image.text if image.status_code != 200 else ''
        assert image.headers['content-type'] == 'image/png'
        assert image.content.startswith(b'\x89PNG\r\n\x1a\n')
        assert client.get(f'/api/documents/{ids["document_id"]}/pages/0/image').status_code == 404
        assert client.get(f'/api/documents/{ids["document_id"]}/pages/2/image').status_code == 404


def test_source_uses_local_pdf_when_database_bytes_are_missing(tmp_path):
    pdf = (ROOT/'samples/indo-global-resolution-order.pdf').read_bytes()
    with TestClient(create_app(tmp_path)) as client:
        ids = client.post('/api/documents', files={'file': ('source.pdf', pdf)}).json()
        with connection(tmp_path, database_url='') as db:
            db.execute('UPDATE documents SET content=NULL WHERE id=?', (ids['document_id'],))
        assert client.get(f'/api/documents/{ids["document_id"]}/content').json()['source_provider'] == 'python'


def test_missing_source_document_returns_404(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/documents/missing/content').status_code == 404
        assert client.get('/api/documents/missing/pages/1/image').status_code == 404
