import time
from io import BytesIO
from urllib.parse import urlsplit, parse_qs

from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from backend.main import ROOT, create_app
from backend.automatic_orders import DEFAULT_CATEGORIES
from backend.db import connection
import hashlib


def listing(category, page=1):
    return f'''<a href="/orders/additional">Additional court</a><table class="reporttable"><tbody>
        <tr><td>1</td><td>29 Sep, 2026</td><td><a href="/uploads/order/{category}-{page}.pdf">Example order</a></td><td>Order</td></tr>
        </tbody></table><ul class="pagination"><li><a href="/orders/{category}?page={2 if category=='nclt' else 1}">Last</a></li></ul>'''.encode()


def wait_job(client, job_id):
    for _ in range(400):
        job = client.get('/api/imports/'+job_id).json()
        if job['status'] not in {'queued','running'}:
            return job
        time.sleep(.02)
    raise AssertionError('Job did not finish')


def test_distinct_source_rows_keep_their_pdf_and_official_subject(tmp_path, monkeypatch):
    original = (ROOT/'samples/indo-global-resolution-order.pdf').read_bytes()
    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(BytesIO(original)))
    writer.add_metadata({'/Title': 'Misleading PDF metadata'})
    output = BytesIO()
    writer.write(output)
    pdfs = {'first.pdf': original, 'second.pdf': output.getvalue()}
    html = b'''<table class="reporttable"><tbody>
        <tr><td>1</td><td>08 Aug, 2022</td><td><a href="/uploads/order/first.pdf">Official first subject</a></td><td>First</td></tr>
        <tr><td>2</td><td>21 Apr, 2022</td><td><a href="/uploads/order/second.pdf">Official second subject</a></td><td>Second</td></tr>
        </tbody></table>'''
    def fetch(client, url, limit):
        name = urlsplit(url).path.rsplit('/', 1)[-1]
        return (pdfs[name] if name in pdfs else html), url
    monkeypatch.setattr('backend.automatic_orders.fetch', fetch)
    with TestClient(create_app(tmp_path)) as client:
        job_id = client.post('/api/scheduler/sync?category=drts').json()['id']
        assert wait_job(client, job_id)['status'] == 'completed'
        rows = client.get('/api/import-files', params={'job_id': job_id}).json()['items']
        combined = client.get('/api/catalogue').json()
        assert combined['total'] == 2
        assert {row['document_id'] for row in combined['items']} == {row['document_id'] for row in rows}
        assert [r['subject'] for r in rows] == ['Official first subject', 'Official second subject']
        assert len({r['document_id'] for r in rows}) == 2
        for row in rows:
            expected = pdfs[row['imported_filename']]
            assert client.get(f'/api/documents/{row["document_id"]}/pdf').content == expected
            assert client.get(f'/api/documents/{row["document_id"]}/content').json()['subject'] == row['subject']
            assert client.get(f'/api/cases/{row["case_id"]}').json()['documents'][0]['subject'] == row['subject']
        with connection(tmp_path, database_url='') as db:
            links = db.execute('SELECT o.document_id,o.pdf_sha256,d.sha256 FROM source_orders o JOIN documents d ON d.id=o.document_id').fetchall()
            assert len(links) == 2
            assert all(r['pdf_sha256'] == r['sha256'] for r in links)


def test_automatic_categories_incremental_and_checkpoints(tmp_path, monkeypatch):
    pdf = (ROOT/'samples/indo-global-resolution-order.pdf').read_bytes()
    downloads = []
    monkeypatch.setattr('backend.automatic_orders.Event.wait', lambda self, timeout=None: self.is_set() if timeout in (.3,.5) else original_wait(self,timeout))
    def fetch(client, url, limit):
        parts = urlsplit(url)
        if parts.path.endswith('.pdf'):
            downloads.append(url)
            return pdf,url
        category = parts.path.rsplit('/',1)[-1]
        page = int(parse_qs(parts.query).get('page',['1'])[0])
        return listing(category,page),url
    monkeypatch.setattr('backend.automatic_orders.fetch',fetch)
    with TestClient(create_app(tmp_path)) as client:
        settings = client.get('/api/scheduler').json()
        assert not settings['enabled'] and len(settings['sources'])==len(DEFAULT_CATEGORIES)
        response=client.post('/api/scheduler/sync')
        assert response.status_code==202
        assert client.post('/api/scheduler/sync').status_code==409
        job=wait_job(client,response.json()['id'])
        assert job['status']=='completed',job
        assert job['result']['imported']==1
        assert len(downloads)==len(DEFAULT_CATEGORIES)+2  # discovered category and second NCLT page
        status=client.get('/api/scheduler').json()
        assert all(s['last_sync'] and s['next_page']==1 for s in status['sources'])
        assert any(s['category']=='additional' for s in status['sources'])
        assert client.get('/api/orders?category=nclt').json()['total']==2
        files=client.get('/api/import-files').json()['items']
        assert all(f['order_kind']!='unknown' for f in files)
        assert all('nclt' in f['categories'] for f in files)
        response=client.post('/api/scheduler/sync')
        assert response.status_code==202
        job=wait_job(client,response.json()['id'])
        assert job['status']=='completed' and job['result']['imported']==0
        assert len(downloads)==len(DEFAULT_CATEGORIES)+2
        assert job['result']['known_orders']==len(downloads)
        assert client.put('/api/scheduler',json={'enabled':False,'interval_minutes':30}).status_code==200
        assert client.get('/api/scheduler').json()['interval_minutes']==30


from threading import Event
original_wait = Event.wait


def test_name_text_and_cin_are_candidates_not_dropped(tmp_path):
    original=(ROOT/'samples/indo-global-resolution-order.pdf').read_bytes()
    writer=PdfWriter()
    writer.clone_document_from_reader(PdfReader(BytesIO(original)))
    writer.add_metadata({'/Title':'Changed container metadata'})
    output=BytesIO()
    writer.write(output)
    with TestClient(create_app(tmp_path)) as client:
        first=client.post('/api/documents',files={'file':('same.pdf',original,'application/pdf')}).json()
        second=client.post('/api/documents',files={'file':('same.pdf',output.getvalue(),'application/pdf')}).json()
        assert second['duplicate'] is False and second['document_id']!=first['document_id']
        rows=client.get('/api/import-files').json()['items']
        candidate=next(r for r in rows if r['document_id']==second['document_id'])['possible_matches'][0]
        assert candidate['document_id']==first['document_id']
        assert {'same_filename','same_day_and_filename','same_extracted_text','shared_CIN'} <= set(candidate['reasons'])


def test_same_official_order_name_and_date_are_review_candidates(tmp_path):
    original=(ROOT/'samples/indo-global-resolution-order.pdf').read_bytes()
    writer=PdfWriter()
    writer.clone_document_from_reader(PdfReader(BytesIO(original)))
    writer.add_metadata({'/Title':'Another PDF container'})
    output=BytesIO()
    writer.write(output)
    with TestClient(create_app(tmp_path)) as client:
        first=client.post('/api/documents',files={'file':('first.pdf',original,'application/pdf')}).json()
        second=client.post('/api/documents',files={'file':('second.pdf',output.getvalue(),'application/pdf')}).json()
        assert second['duplicate'] is False
        with connection(tmp_path, database_url='') as db:
            for index,item in enumerate((first,second),1):
                db.execute('''INSERT INTO source_orders
                    (id,pdf_url,order_date,subject,remarks,first_seen_at,last_seen_at,document_id,case_id)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                    (f'order-{index}',f'https://ibbi.gov.in/uploads/order/{index}.pdf',
                     '2026-09-29','The same official order','Order','2026-09-30','2026-09-30',
                     item['document_id'],item['case_id']))
        rows=client.get('/api/import-files').json()['items']
        candidate=next(row for row in rows if row['document_id']==second['document_id'])['possible_matches']
        matched=next(row for row in candidate if row['document_id']==first['document_id'])
        assert {'same_official_order_name','same_order_date_and_name'} <= set(matched['reasons'])


def test_failed_page_does_not_advance_sync_date(tmp_path,monkeypatch):
    def fail(*args):
        raise ValueError('Temporary failure')
    monkeypatch.setattr('backend.automatic_orders.fetch',fail)
    with TestClient(create_app(tmp_path)) as client:
        job_id=client.post('/api/scheduler/sync').json()['id']
        job=wait_job(client,job_id)
        assert job['status']=='completed_with_errors'
        assert all(not s['last_sync'] and s['status']=='failed' for s in client.get('/api/scheduler').json()['sources'])


def test_category_selection_and_saved_schedule(tmp_path,monkeypatch):
    pdf=(ROOT/'samples/indo-global-resolution-order.pdf').read_bytes()
    urls=[]
    def fetch(client,url,limit):
        urls.append(url)
        if url.endswith('.pdf'): return pdf,url
        return listing(urlsplit(url).path.rsplit('/',1)[-1]),url
    monkeypatch.setattr('backend.automatic_orders.fetch',fetch)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/scheduler').json()['category']=='all'
        assert client.put('/api/scheduler',json={'enabled':False,'category':'drts','interval_minutes':30}).status_code==200
        assert client.get('/api/scheduler').json()['category']=='drts'
        response=client.post('/api/scheduler/sync?category=drts')
        job=wait_job(client,response.json()['id'])
        assert job['result']['category']=='drts' and job['result']['imported']==1
        sources=client.get('/api/scheduler').json()['sources']
        assert [s['category'] for s in sources if s['last_sync']]==['drts']
        assert all('/drts?' in url for url in urls if '?' in url)
        assert client.post('/api/scheduler/sync?category=invalid').status_code==422
