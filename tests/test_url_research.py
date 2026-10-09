import socket
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import url_import
from backend.db import connection
from backend.main import ROOT, create_app


def test_public_addresses_and_redirects(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 80))])
    for url in ('http://localhost/a', 'file:///x', 'http://user:password@example.com'):
        with pytest.raises(ValueError):
            url_import.public_url(url)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, '', ('8.8.8.8' if a[0] == 'public.example' else '127.0.0.1', 80))])
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={'location':'http://localhost/secret'}))) as client:
        with pytest.raises(ValueError, match='Private'):
            url_import.fetch(client, 'https://public.example', 100)


def test_import_pagination_duplicates_failures_and_jobs(tmp_path, monkeypatch):
    pdf = (ROOT / 'samples/indo-global-resolution-order.pdf').read_bytes()
    pages = {
        'https://public.example/': b'<a href="one.pdf">PDF</a><a rel="next" href="/?page=2">Next</a>',
        'https://public.example/?page=2': b'<a href="one.pdf">Same link</a><a href="two.pdf">Same content</a><a href="bad.pdf">Bad</a>',
        'https://public.example/one.pdf': pdf,
        'https://public.example/two.pdf': b'<html>loader</html>' + pdf,
        'https://public.example/bad.pdf': b'not a PDF',
    }
    monkeypatch.setattr(url_import, 'public_url', lambda u: u)
    monkeypatch.setattr('backend.main.public_url', lambda u: u)
    monkeypatch.setattr(url_import, 'fetch', lambda client, url, limit: (pages[url], url))
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/imports', json={'url':'https://public.example/', 'max_files':25})
        assert response.status_code == 202
        job_id = response.json()['id']
        for _ in range(200):
            job = client.get('/api/imports/'+job_id).json()
            if job['status'] not in ('queued','running'):
                break
            time.sleep(.02)
        assert job['status'] == 'completed_with_errors', job
        assert job['result']['imported'] == 1
        assert job['result']['duplicates'] == 1
        assert job['result']['items'][1]['removed_prefix_bytes'] == 19
        assert job['result']['failed'] == 1
        assert job['result']['pages'] == 2
        assert len(client.get('/api/cases').json()) == 1
        files = client.get('/api/import-files').json()
        assert files['total'] == 2
        combined = client.get('/api/catalogue').json()
        assert combined['total'] == 2
        assert all(row['source_order_id'] is None and row['document_id'] for row in combined['items'])
        assert client.get('/api/catalogue?duplicate=yes').json()['total'] == 1
        assert client.get('/api/catalogue?duplicate=no').json()['total'] == 1
        assert all(f['subject'] for f in files['items'])
        assert sorted(f['duplicate'] for f in files['items']) == [0, 1]
        duplicate_file = next(f for f in files['items'] if f['duplicate'])
        original = duplicate_file['duplicate_of']
        assert duplicate_file['imported_filename'] == 'two.pdf'
        assert duplicate_file['source_url'] == 'https://public.example/two.pdf'
        assert original['document_id'] == duplicate_file['document_id']
        assert original['filename'] == 'one.pdf'
        assert original['source_url'] == 'https://public.example/one.pdf'
        assert client.get('/api/documents/'+original['document_id']+'/pdf').content == pdf
        assert next(f for f in files['items'] if not f['duplicate'])['duplicate_of'] is None
        assert all(f['created_at'] and f['document_id'] for f in files['items'])
        assert client.get('/api/import-files?duplicate=yes').json()['total'] == 1
        assert client.get('/api/import-files?page_size=1&page=2').json()['items']
        document_id = files['items'][0]['document_id']
        assert client.get('/api/documents/'+document_id+'/content').json()['pages']
        assert client.get('/api/documents/'+document_id+'/pdf').content == pdf
        assert client.get('/api/imports').json()[0]['id'] == job_id
        history = client.get('/api/import-history').json()
        assert history['total'] == 1
        task = history['items'][0]
        assert task['id'] == job_id and task['created_at']
        assert task['imported'] == 1 and task['duplicates'] == 1 and task['failed'] == 1
        assert task['status'] == 'completed_with_errors'
        assert task['subjects']
        assert client.get('/api/import-files?job_id='+job_id).json()['total'] == 2
        assert client.get('/api/import-files?job_id=nonexistent').json()['total'] == 0
        assert client.get('/api/imports/missing').status_code == 404
        assert client.post('/api/imports', json={'url':'https://public.example/', 'max_files':501}).status_code == 422


def test_download_retry_and_size_limit(monkeypatch):
    monkeypatch.setattr(url_import, 'public_url', lambda u: u)
    monkeypatch.setattr(url_import.time, 'sleep', lambda _: None)
    attempts = []
    def response(request):
        attempts.append(request.url)
        return httpx.Response(503 if len(attempts) < 3 else 200, content=b'12345')
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert url_import.fetch(client, 'https://public.example/file', 5)[0] == b'12345'
        assert len(attempts) == 3
        with pytest.raises(ValueError, match='size limit'):
            url_import.fetch(client, 'https://public.example/file', 4)


def test_stops_at_file_limit_with_unlimited_pages(monkeypatch):
    pages = {'https://public.example/': b'<a href="a.pdf">PDF</a><a rel="next" href="/?page=2">Next</a>',
             'https://public.example/?page=2': b'<a href="b.pdf">PDF</a>',
             'https://public.example/a.pdf': b'%PDF-test'}
    monkeypatch.setattr(url_import, 'fetch', lambda client, url, limit: (pages[url], url))
    result = url_import.crawl(url_import.ImportRequest(url='https://public.example/', max_pages=None, max_files=1),
        lambda *a: {'document_id':'doc','case_id':'case','duplicate':False}, lambda _: None)
    assert result['pages'] == 1
    assert result['imported'] == 1
    assert result['limits_reached']


def test_structured_tables_matching_and_analytics(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        case_id = client.post('/api/sample/import').json()['case_id']
        detail = client.get('/api/cases/'+case_id).json()
        record = detail['record']
        doc = detail['documents'][0]['id']
        fact = lambda value: {'value':value, 'document_id':doc, 'page':1}
        record['admission_date'] = fact('2020-01-01')
        record['resolution_date'] = fact('2020-04-10')
        record['previous_directors'] = [{**fact('Sahil Kumar'), 'din':'12345678'}]
        record['subsequent_directors'] = [{**fact('S. Kumar'), 'din':'12345678'}]
        record['plans'] = [{'applicant':'Buyer', 'amount':'20', 'admitted':'100', 'document_id':doc, 'page':1}]
        saved = client.put('/api/cases/'+case_id, json={'version':1, 'record':record})
        assert saved.status_code == 200, saved.text
        matches = client.get('/api/entities/match?din=12345678').json()
        assert len(matches) == 1 and matches[0]['match'] == 'identifier'
        assert client.get('/api/entities/match?name=Sahil%20Kumar').json()[0]['match'] == 'name_candidate_requires_review'
        with connection(tmp_path, database_url='') as db:
            assert db.execute('SELECT COUNT(*) FROM claims').fetchone()[0] == 4
            assert db.execute('SELECT haircut FROM plans').fetchone()[0] == 80
            people = db.execute('SELECT entity_id FROM people').fetchall()
            assert people[0][0] == people[1][0]
        assert client.post('/api/cases/'+case_id+'/approve', json={'version':2,'reviewer':'Tester','confirmed':True}).status_code == 200
        stats = client.get('/api/analytics').json()
        assert stats['bench_timelines'][0]['average_days'] == 100
        assert stats['professional_haircuts'][0]['average_haircut_percent'] == '83.77'
        assert len(stats['ownership_overlap'][0]['confirmed']) == 1
        record['resolution_date'] = fact('2019-01-01')
        assert client.put('/api/cases/'+case_id, json={'version':3,'record':record}).status_code == 422
