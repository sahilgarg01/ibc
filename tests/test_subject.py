import json
from backend.extraction import pdf_subject
from backend.db import connection, initialize
from backend.main import create_app
from fastapi.testclient import TestClient


def test_subject_sources():
    pages = [{'page':1, 'text':'Subject: Resolution approval for Example Ltd\nOrder'}]
    assert pdf_subject(pages)[0] == 'Resolution approval for Example Ltd'
    assert pdf_subject(pages, {'/Subject':'Company resolution'}) == ('Company resolution','pdf_metadata')
    assert pdf_subject([{'page':1,'text':'In the matter of\nExample Ltd\nResolution plan'}])[0].startswith('In the matter of Example Ltd')
    assert pdf_subject([{'page':1,'text':''}]) == (None,'unavailable')


def test_existing_subject_backfill(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.post('/api/sample/import')
        assert client.get('/api/cases').json()[0]['subjects']
    with connection(tmp_path, database_url='') as db:
        row = db.execute('SELECT id,extraction FROM documents').fetchone()
        extraction = json.loads(row['extraction'])
        extraction.pop('subject', None)
        extraction.pop('subject_source', None)
        db.execute('UPDATE documents SET subject=NULL,extraction=? WHERE id=?', (json.dumps(extraction),row['id']))
    initialize(tmp_path, database_url='')
    with connection(tmp_path, database_url='') as db:
        row = db.execute('SELECT subject,extraction FROM documents').fetchone()
        assert row['subject']
        assert json.loads(row['extraction'])['subject'] == row['subject']
