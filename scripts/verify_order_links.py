"""Verify every stored DRTs row against its current public listing and PDF bytes.

Only verified source metadata/links are repaired. No valid document is deleted.
"""
import hashlib
import json
import httpx
from backend.db import ROOT, connection, initialize
from backend.ibbi_orders import BASE, parse_listing
from backend.url_import import fetch


def main():
    initialize(ROOT / 'data')
    with connection(ROOT / 'data') as db:
        settings = db.execute('SELECT active_job FROM scheduler_settings WHERE id=1').fetchone()
        if settings['active_job']:
            raise SystemExit('Wait for the running import before verifying links.')
    checked, issues = [], []
    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
        page, last = 1, 1
        while page <= last:
            html, _ = fetch(client, f'{BASE}/orders/drts?page={page}', 15*1024*1024)
            rows, last, _ = parse_listing(html.decode('utf-8', errors='replace'))
            for order in rows:
                with connection(ROOT / 'data') as db:
                    stored = db.execute('SELECT d.id,d.sha256,d.content,o.id order_id FROM source_orders o JOIN documents d ON d.id=o.document_id WHERE o.pdf_url=?', (order['pdf_url'],)).fetchone()
                    events = db.execute('SELECT DISTINCT document_id FROM import_files WHERE source_url=?', (order['pdf_url'],)).fetchall()
                if not stored:
                    issues.append({'url': order['pdf_url'], 'error': 'No explicit stored document link'})
                    continue
                pdf, _ = fetch(client, order['pdf_url'], 50*1024*1024)
                prefix = pdf.find(b'%PDF-', 0, 8192)
                if prefix > 0:
                    pdf = pdf[prefix:]
                digest = hashlib.sha256(pdf).hexdigest()
                if digest != stored['sha256'] or hashlib.sha256(bytes(stored['content'])).hexdigest() != digest or any(e[0] != stored['id'] for e in events):
                    issues.append({'url': order['pdf_url'], 'error': 'Source, stored bytes or import event mismatch'})
                    continue
                with connection(ROOT / 'data') as db:
                    db.execute('UPDATE source_orders SET subject=?,order_date=?,remarks=?,pdf_sha256=?,pdf_bytes=? WHERE id=?',
                               (order['subject'],order['order_date'],order['remarks'],digest,len(pdf),stored['order_id']))
                checked.append({'subject': order['subject'], 'url': order['pdf_url'], 'document_id': stored['id'], 'sha256': digest})
                print(f'Verified {len(checked)}: {order["subject"]}', flush=True)
            page += 1
    report = {'category': 'drts', 'verified': len(checked), 'distinct_pdfs': len({r['sha256'] for r in checked}), 'issues': issues, 'rows': checked}
    # No lifespan context: verification must not start the background scheduler.
    from fastapi.testclient import TestClient
    from backend.main import create_app
    client = TestClient(create_app())
    files = client.get('/api/import-files', params={'page_size': 100}).json()['items']
    for verified in checked:
        matching = [row for row in files if row['source_url'] == verified['url']]
        if not matching or any(row['subject'] != verified['subject'] or row['document_id'] != verified['document_id'] for row in matching):
            issues.append({'url': verified['url'], 'error': 'Displayed row mismatch'})
        response = client.get(f'/api/documents/{verified["document_id"]}/pdf')
        if response.status_code != 200 or hashlib.sha256(response.content).hexdigest() != verified['sha256']:
            issues.append({'url': verified['url'], 'error': 'PDF endpoint mismatch'})
    client.close()
    (ROOT / 'data' / 'order-link-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='rows'}), flush=True)
    if issues:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
