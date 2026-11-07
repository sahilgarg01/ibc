import copy
import logging

from backend import url_import
from backend.logging_config import PollingAccessFilter


def test_discovery_is_published_before_any_pdf_processing(monkeypatch):
    html = b'<a href="/first.pdf">A</a><a href="/second.pdf">B</a>'
    snapshots = []
    fetched = []

    def fetch(client, url, limit):
        fetched.append(url)
        return (b'%PDF-test' if url.endswith('.pdf') else html), url

    def ingest(*args):
        assert snapshots[-1]['discovered'] == 2
        assert snapshots[-1]['phase'] == 'processing'
        return {'duplicate': False}

    monkeypatch.setattr(url_import, 'fetch', fetch)
    result = url_import.crawl(url_import.ImportRequest(url='https://example.com/orders'),
                              ingest, lambda value: snapshots.append(copy.deepcopy(value)))
    assert result['discovered'] == result['processed'] == result['imported'] == 2
    assert result['phase'] == 'finished'
    assert result['current_file'] is None
    assert any(s['discovered'] == 2 and s['processed'] == 0 for s in snapshots)


def test_poll_filter_retains_errors_and_nonpoll_requests():
    poll_filter = PollingAccessFilter()

    def record(method, path, status):
        return logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s %s %s %s %s',
                                 ('local', method, path, '1.1', status), None)

    assert not poll_filter.filter(record('GET', '/api/import-files?page=1', 200))
    assert not poll_filter.filter(record('GET', '/api/imports/job', 200))
    assert poll_filter.filter(record('GET', '/api/imports/job', 500))
    assert poll_filter.filter(record('POST', '/api/imports', 200))
    assert poll_filter.filter(record('GET', '/api/documents/id/pdf', 200))
