"""Verify public final-order PDFs through manual import and sync in temporary databases.

Run: .venv/Scripts/python.exe -m scripts.verify_final_imports
Downloads two public PDFs into memory; never changes the configured application database.
"""
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.main import create_app


PDFS = {
    'liquidation': 'https://ibbi.gov.in/uploads/order/60648f1eb69189928a51539857b5dac4.pdf',
    'resolution': 'https://ibbi.gov.in/uploads/order/e3fb9fd83bebfde5b121ca0bccbbc2f6.pdf',
}


def main():
    downloaded = {}
    with httpx.Client(timeout=60, follow_redirects=True, trust_env=False) as client:
        for kind, url in PDFS.items():
            response = client.get(url)
            response.raise_for_status()
            downloaded[url] = response.content
    html = '<table class="reporttable"><tbody>'
    for index, (kind, url) in enumerate(PDFS.items(), 1):
        remark = 'Appointment of Liquidator' if kind == 'liquidation' else 'Approval of resolution plan'
        html += f'<tr><td>{index}</td><td>15 Oct, 2026</td><td><a href="{url}">{kind} order</a></td><td>{remark}</td></tr>'
    html += '</tbody></table>'

    def fetch(client, url, limit):
        return downloaded.get(url, html.encode()), url

    for method in ('manual', 'sync'):
        with TemporaryDirectory(prefix='ibc-final-import-') as temporary:
            with patch('backend.url_import.fetch', fetch), patch('backend.automatic_orders.fetch', fetch), \
                    patch('backend.main.public_url', lambda url: url):
                with TestClient(create_app(Path(temporary))) as client:
                    response = (client.post('/api/imports', json={'url': 'https://ibbi.gov.in/orders/nclt', 'max_files': 10})
                                if method == 'manual' else client.post('/api/scheduler/sync?category=nclt'))
                    response.raise_for_status()
                    job_id = response.json()['id']
                    for _ in range(1200):
                        job = client.get('/api/imports/'+job_id).json()
                        if job['status'] not in {'queued', 'running'}:
                            break
                        time.sleep(.1)
                    assert job['status'] == 'completed', job
                    assert job['result']['imported'] == 2, job
                    cases = client.get('/api/cases').json()
                    outcomes = set()
                    for case in cases:
                        detail = client.get('/api/cases/'+case['id']).json()
                        assert detail['review_status'] == 'draft'
                        assert detail['record']['outcome_evidence']['page'] > 0
                        outcomes.add(detail['record']['outcome'])
                    assert outcomes == {'resolution_approved', 'liquidation_ordered'}, outcomes
                    print(method, 'PASS: two real PDFs imported, two review drafts, both outcomes with page evidence', flush=True)


if __name__ == '__main__':
    main()
