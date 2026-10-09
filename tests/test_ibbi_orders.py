import httpx
from fastapi.testclient import TestClient

from backend.db import connection
from backend.ibbi_orders import parse_listing, sync_listings
from backend.main import ROOT, create_app


def page_html(number, pdf_name, last=2):
    return f"""<form action='/orders/nclt'><input name='title'><input name='date'>
      <input type='radio' name='adverse_against' value='ip'>
      <select name='nclt'><option value='38'>Mumbai Bench</option></select></form>
      <table class='reporttable'><tbody><tr><td>{number}</td><td>23 Sep, 2026</td>
      <td><a href='/uploads/order/{pdf_name}.pdf'>In the matter of TEST COMPANY {number} (25 KB)</a></td>
      <td>Approval of Resolution Plan</td></tr></tbody></table>
      <ul class='pagination'><li class='last'><a href='/orders/nclt?page={last}'>Last</a></li></ul>"""


def test_listing_sync_resume_and_catalogue(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        page = int(request.url.params.get('page', 1))
        return httpx.Response(200, text=page_html(page, f'fixture-{page}'))

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        result = sync_listings(tmp_path, limit_pages=2, delay=0, client=client)
        assert result['last_page'] == 2
        assert result['pages_fetched'] == 2
        assert sync_listings(tmp_path, limit_pages=2, delay=0, client=client)['pages_fetched'] == 1
        assert sync_listings(tmp_path, adverse='ip', limit_pages=1, delay=0, client=client)['pages_fetched'] == 1
    assert len(requests) == 4  # first page refreshes, page two is checkpointed

    with connection(tmp_path) as db:
        assert db.execute('SELECT COUNT(*) FROM source_orders').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM source_pages').fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM source_order_filters').fetchone()[0] == 1

    # A source listing becomes an unapproved review draft only when selected.
    monkeypatch.setattr('backend.main.download_order', lambda root, order_id: ROOT / 'samples/indo-global-resolution-order.pdf')
    with TestClient(create_app(tmp_path)) as api:
        stats = api.get('/api/orders/stats').json()
        assert stats['orders'] == 2 and stats['pages_fetched'] == 2
        assert api.get('/api/orders/summary').json()['by_year'][0]['orders'] == 2
        orders = api.get('/api/orders?q=TEST COMPANY 1').json()
        assert orders['total'] == 1 and orders['items'][0]['remarks'] == 'Approval of Resolution Plan'
        assert api.get('/api/orders?adverse=ip').json()['total'] == 1
        assert api.get('/api/orders/filters').json()['benches'][0]['key'] == '38'
        order_id = orders['items'][0]['id']
        combined = api.get('/api/catalogue').json()
        assert combined['total'] == 2
        assert all(row['document_id'] is None and row['source_order_id'] for row in combined['items'])
        assert api.get('/api/catalogue?q=TEST COMPANY 1').json()['total'] == 1
        assert api.get('/api/catalogue?adverse=ip').json()['total'] == 1
        review = api.post(f'/api/orders/{order_id}/review')
        assert review.status_code == 200, review.text
        combined = api.get('/api/catalogue').json()
        assert combined['total'] == 2
        linked = next(row for row in combined['items'] if row['source_order_id'] == order_id)
        assert linked['document_id'] == review.json()['document_id']
        assert linked['subject'] == 'In the matter of TEST COMPANY 1'
        assert linked['pdf_status'] == 'downloaded'
        case = api.get(f"/api/cases/{review.json()['case_id']}").json()
        assert case['review_status'] == 'draft'
        assert case['documents'][0]['source_url'].endswith('fixture-1.pdf')
        assert api.get('/api/analytics').json()['approved_cases'] == 0


def test_parser_refuses_changed_page():
    assert parse_listing(page_html(1, 'abc'))[0][0]['pdf_url'].endswith('/abc.pdf')
    try:
        parse_listing('<html><body>Access denied</body></html>')
    except ValueError as error:
        assert 'table missing' in str(error)
    else:
        raise AssertionError('Changed source layout must stop the import')


def test_unlinked_manual_pdf_with_same_official_url_remains_visible(tmp_path):
    pdf = (ROOT / 'samples/indo-global-resolution-order.pdf').read_bytes()
    url = 'https://ibbi.gov.in/uploads/order/manual-test.pdf'
    with TestClient(create_app(tmp_path)) as api:
        result = api.post('/api/documents', files={'file':('manual.pdf',pdf,'application/pdf')},
                          data={'source_url':url}).json()
        with connection(tmp_path) as db:
            db.execute('''INSERT INTO source_orders
                (id,pdf_url,subject,remarks,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?)''', ('manual-order',url,'Official subject','',
                                          '2026-09-30T00:00:00+00:00','2026-09-30T00:00:00+00:00'))
        rows = api.get('/api/catalogue').json()['items']
        assert len(rows) == 2
        assert any(row['source_order_id'] is None and row['document_id'] == result['document_id'] for row in rows)
        with connection(tmp_path) as db:
            db.execute('UPDATE source_orders SET document_id=?,case_id=? WHERE id=?',
                       (result['document_id'],result['case_id'],'manual-order'))
        rows = api.get('/api/catalogue').json()['items']
        assert len(rows) == 1
        assert rows[0]['subject'] == 'Official subject'


def test_alternate_court_columns_and_double_slash_pdf():
    html = '''<table class="reporttable"><tbody><tr><td>1</td><td>Authority</td>
    <td>29 Sep, 2026</td><td><a href="https://ibbi.gov.in//uploads/order/file.pdf">Court subject</a></td></tr>
    </tbody></table>'''
    order = parse_listing(html)[0][0]
    assert order['pdf_url']=='https://ibbi.gov.in/uploads/order/file.pdf'
    assert order['order_date']=='2026-09-29' and order['subject']=='Court subject'
