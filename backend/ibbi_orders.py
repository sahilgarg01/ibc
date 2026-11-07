"""Resumable importer for the public IBBI NCLT order listing and linked PDFs."""
import argparse
import hashlib
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
import httpx
from bs4 import BeautifulSoup
from .db import connection, initialize
BASE = 'https://ibbi.gov.in'
LISTING = f'{BASE}/orders/nclt'
USER_AGENT = 'IBC-Evidence-Explorer/0.2 (research importer; contact: local operator)'
MAX_PDF_BYTES = 50 * 1024 * 1024
ORDER_REMARK_TERMS = ('liquidation', 'final order', 'sale as going concern', 'sale as a going concern')


def matches_order_remarks(remarks):
    """Match listing remarks, ignoring case and repeated whitespace."""
    normalized = ' '.join((remarks or '').casefold().split())
    return any(term in normalized for term in ORDER_REMARK_TERMS)

class IbbiOrders:

    @staticmethod
    def utc_now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def safe_pdf_url(url: str) -> str:
        parsed = urlparse(urljoin(BASE, url))
        if parsed.scheme != 'https' or parsed.hostname not in {'ibbi.gov.in', 'www.ibbi.gov.in'}:
            raise ValueError('PDF URL must be hosted by IBBI')
        path = re.sub('/+', '/', parsed.path)
        if not path.startswith('/uploads/order/') or not path.lower().endswith('.pdf'):
            raise ValueError('PDF URL is outside the IBBI orders directory')
        return parsed._replace(path=path, query='', fragment='').geturl()

    @staticmethod
    def parse_listing(html: str, *, allow_empty=False):
        soup = BeautifulSoup(html, 'html.parser')
        table = soup.select_one('table.reporttable')
        if table is None:
            raise ValueError('IBBI orders table missing; the page may have changed')
        rows = []
        headers = [re.sub(r'[^a-z]', '', cell.get_text(' ', strip=True).casefold())
                   for cell in table.select('thead tr:last-child th, thead tr:last-child td')]
        remarks_index = next((i for i, name in enumerate(headers)
                              if name in {'orderremarks', 'ordersremarks', 'remarks'}), None)
        for tr in table.select('tbody tr'):
            cells = tr.find_all('td', recursive=False)
            if len(cells) < 3:
                continue
            anchor = next((a for a in tr.select('a[href]') if '.pdf' in a['href'].lower()), None)
            if not anchor:
                continue
            try:
                pdf_url = safe_pdf_url(anchor['href'])
            except ValueError:
                continue
            order_date = None
            for cell in cells:
                date_text = cell.get_text(' ', strip=True)
                for fmt in ('%d %b, %Y', '%d %B, %Y', '%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
                    try:
                        order_date = datetime.strptime(date_text, fmt).date().isoformat()
                        break
                    except ValueError:
                        continue
                if order_date:
                    break
            subject = re.sub(
                '\\s*\\([\\d,.]+\\s*(?:KB|MB)\\)\\s*$',
                '',
                anchor.get_text(' ', strip=True),
                flags=re.I,
            )
            rows.append(
                {
                    'id': hashlib.sha256(pdf_url.encode()).hexdigest()[:24],
                    'pdf_url': pdf_url,
                    'order_date': order_date,
                    'subject': subject,
                    'remarks': (cells[remarks_index].get_text(' ', strip=True)
                                if remarks_index is not None and remarks_index < len(cells)
                                else cells[-1].get_text(' ', strip=True) if not cells[-1].find('a', href=True) else ''),
                },
            )
        if not rows and (not allow_empty):
            raise ValueError('No PDF order rows found; refusing to record an empty page')
        last_page = 1
        for a in soup.select('ul.pagination a[href]'):
            values = parse_qs(urlparse(a['href']).query).get('page', [])
            if values and values[0].isdigit():
                last_page = max(last_page, int(values[0]))
        benches = {option.get('value'): option.get_text(' ', strip=True) for option in soup.select('select[name="nclt"] option') if option.get('value')}
        return (rows, last_page, benches)

    @staticmethod
    def fetch(client: httpx.Client, url: str, *, stream_limit: int | None=None):
        for attempt in range(5):
            try:
                with client.stream('GET', url) as response:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        raise httpx.HTTPStatusError(
                            'temporary IBBI response',
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    if urlparse(str(response.url)).hostname not in {'ibbi.gov.in', 'www.ibbi.gov.in'}:
                        raise ValueError('Unexpected redirect outside IBBI')
                    chunks = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if stream_limit is not None and total > stream_limit:
                            raise ValueError(f'PDF exceeds {stream_limit} bytes')
                        chunks.append(chunk)
                    return b''.join(chunks)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {429, 500, 502, 503, 504}:
                    raise
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 8))

    @staticmethod
    def listing_url(page: int, bench_id: str | None=None, adverse: str | None=None):
        query = {'page': page}
        if bench_id:
            query['nclt'] = bench_id
        if adverse:
            query['adverse_against'] = adverse
        return LISTING + '?' + urlencode(query)

    @staticmethod
    def sync_listings(
        root: Path,
        *,
        bench_id: str | None=None,
        adverse: str | None=None,
        limit_pages: int | None=None,
        refresh: bool=False,
        delay: float=0.5,
        workers: int=3,
        client: httpx.Client | None=None,
    ):
        initialize(root)
        if adverse not in {None, 'ip', 'other'}:
            raise ValueError('Adverse filter must be ip or other')
        key = ':'.join(
            (part for part in (f'bench:{bench_id}' if bench_id else None, f'adverse:{adverse}' if adverse else None) if part),
        ) or 'all'
        owned = client is None
        client = client or httpx.Client(headers={'User-Agent': USER_AGENT}, follow_redirects=True, timeout=45)
        fetched = 0
        try:
            first_html = fetch(client, listing_url(1, bench_id, adverse)).decode('utf-8', errors='replace')
            first_rows, total_pages, benches = parse_listing(first_html)
            if bench_id and bench_id not in benches:
                raise ValueError(f'Unknown IBBI bench filter {bench_id}')
            with connection(root) as db:
                stamp = utc_now()
                for option_id, name in benches.items():
                    db.upsert_listing_filter(option_id, name, stamp, stamp)
            end = min(total_pages, limit_pages) if limit_pages else total_pages
            with connection(root) as db:
                saved_pages = {r['page_number'] for r in db.saved_source_pages(key)}
            pending = [p for p in range(2, end + 1) if refresh or p not in saved_pages]

            def load_page(page):
                data = fetch(client, listing_url(page, bench_id, adverse))
                if delay:
                    time.sleep(delay)
                rows, current_last, _ = parse_listing(data.decode('utf-8', errors='replace'))
                return (page, rows, current_last)

            def save_page(page, rows, current_last):
                nonlocal fetched
                stamp = utc_now()
                with connection(root) as db:
                    db.begin()
                    for row in rows:
                        db.upsert_listing_order(
                            row['id'],
                            row['pdf_url'],
                            row['order_date'],
                            row['subject'],
                            row['remarks'],
                            bench_id,
                            benches.get(bench_id) if bench_id else None,
                            stamp,
                            stamp,
                        )
                        if adverse:
                            db.add_source_order_filter(row['id'], f'adverse:{adverse}')
                    db.save_listing_page(key, page, stamp, len(rows), current_last)
                    db.clear_source_page_failure(key, page)
                fetched += 1
                if page == 1 or fetched % 25 == 0 or page == end:
                    print(
                        f'{key}: page {page}/{total_pages}, fetched this run {fetched}, rows {len(rows)}',
                        flush=True,
                    )
            save_page(1, first_rows, total_pages)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                jobs = {pool.submit(load_page, page): page for page in pending}
                for job in as_completed(jobs):
                    page = jobs[job]
                    try:
                        _, rows, current_last = job.result()
                        save_page(page, rows, current_last)
                    except (httpx.HTTPError, ValueError, OSError) as exc:
                        with connection(root) as db:
                            db.record_listing_page_failure(key, page, str(exc)[:1000], utc_now())
                        print(f'{key}: page {page} failed: {exc}', flush=True)
        finally:
            if owned:
                client.close()
        return {
            'filter': key,
            'pages_fetched': fetched,
            'last_page': total_pages,
            'benches': benches if not bench_id else None,
        }

    @staticmethod
    def download_order(root: Path, order_id: str, client: httpx.Client | None=None):
        initialize(root)
        with connection(root) as db:
            row = db.source_order(order_id)
            if not row:
                raise ValueError('Order not found')
            if row['pdf_status'] == 'downloaded' and (root / 'source_documents' / f'{order_id}.pdf').exists():
                return root / 'source_documents' / f'{order_id}.pdf'
            url = safe_pdf_url(row['pdf_url'])
        owned = client is None
        client = client or httpx.Client(headers={'User-Agent': USER_AGENT}, follow_redirects=True, timeout=90)
        try:
            raw = fetch(client, url, stream_limit=MAX_PDF_BYTES + 4096)
            prefix = raw.find(b'%PDF-', 0, 4096)
            if prefix < 0:
                raise ValueError('IBBI response contains no PDF header')
            content = raw[prefix:]
            folder = root / 'source_documents'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f'{order_id}.pdf'
            part = folder / f'{order_id}.part'
            part.write_bytes(content)
            part.replace(path)
            with connection(root) as db:
                db.complete_pdf_download(
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                    prefix,
                    utc_now(),
                    order_id,
                )
            return path
        finally:
            if owned:
                client.close()

    @staticmethod
    def sync_pdfs(
        root: Path,
        *,
        limit: int | None=None,
        delay: float=0.5,
        retry_failed: bool=False,
    ):
        initialize(root)
        with connection(root) as db:
            ids = db.pending_pdf_ids(retry_failed, limit)
        success = failed = 0
        with httpx.Client(headers={'User-Agent': USER_AGENT}, follow_redirects=True, timeout=90) as client:
            for index, order_id in enumerate(ids, 1):
                if shutil.disk_usage(root).free < 5 * 1024 ** 3:
                    print(
                        'Stopping PDF downloads: less than 5 GB of free space remains',
                        flush=True,
                    )
                    break
                try:
                    download_order(root, order_id, client)
                    success += 1
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    failed += 1
                    with connection(root) as db:
                        db.fail_order_import(str(exc)[:1000], order_id)
                    print(f'PDF failed {order_id}: {exc}', flush=True)
                if index % 25 == 0 or index == len(ids):
                    print(
                        f'PDFs {index}/{len(ids)}: downloaded {success}, failed {failed}',
                        flush=True,
                    )
                if index < len(ids):
                    time.sleep(delay)
        return {'downloaded': success, 'failed': failed}

    @staticmethod
    def follow_pdfs(root: Path, *, batch_size: int=25, delay: float=0.5):
        """Keep downloading newly discovered orders until the global listing is complete."""
        initialize(root)
        while True:
            with connection(root) as db:
                pending = db.pending_pdf_count()[0]
                page_status = db.global_page_progress()
            if pending:
                result = sync_pdfs(root, limit=batch_size, delay=delay)
                if not result['downloaded'] and (not result['failed']):
                    break
            elif page_status[1] and page_status[0] >= page_status[1]:
                print('Global listing and PDF queue complete', flush=True)
                break
            else:
                time.sleep(30)

    @staticmethod
    def import_status(root: Path):
        initialize(root)
        with connection(root) as db:
            orders = db.listing_import_counts()
            pages = db.global_source_page_counts()
            failed_pages = db.global_source_page_failure_count()[0]
            benches = db.synced_bench_count()[0]
        return {
            'orders': orders['total'],
            'listing_pages': pages['done'],
            'listing_pages_reported': pages['expected'],
            'listing_pages_failed': failed_pages,
            'benches_synced': benches,
            'pdfs_downloaded': orders['downloaded'],
            'pdfs_failed': orders['failed'],
            'orders_in_review': orders['reviewed'],
        }
utc_now = IbbiOrders.utc_now
safe_pdf_url = IbbiOrders.safe_pdf_url
parse_listing = IbbiOrders.parse_listing
fetch = IbbiOrders.fetch
listing_url = IbbiOrders.listing_url
sync_listings = IbbiOrders.sync_listings
download_order = IbbiOrders.download_order
sync_pdfs = IbbiOrders.sync_pdfs
follow_pdfs = IbbiOrders.follow_pdfs
import_status = IbbiOrders.import_status

def main():
    parser = argparse.ArgumentParser(description='IBBI NCLT public orders importer')
    parser.add_argument('action', choices=['sync', 'sync-pdfs', 'follow-pdfs', 'status'])
    parser.add_argument(
        '--data-dir',
        type=Path,
        default=Path(__file__).resolve().parents[1] / 'data',
    )
    parser.add_argument('--bench', help='IBBI NCLT filter ID, e.g. 38 for Mumbai')
    parser.add_argument(
        '--adverse',
        choices=['ip', 'other'],
        help='Site radio filter: adverse against IP or other',
    )
    parser.add_argument('--max-pages', type=int)
    parser.add_argument('--limit', type=int, help='Maximum PDFs for sync-pdfs')
    parser.add_argument(
        '--refresh',
        action='store_true',
        help='Refetch previously saved listing pages',
    )
    parser.add_argument(
        '--retry-failed',
        action='store_true',
        help='Retry PDFs whose earlier download failed',
    )
    parser.add_argument('--delay', type=float, default=0.5)
    parser.add_argument(
        '--workers',
        type=int,
        default=3,
        help='Concurrent listing requests (default 3)',
    )
    args = parser.parse_args()
    if args.delay < 0 or args.workers < 1 or args.workers > 5 or (args.max_pages is not None and args.max_pages < 1) or (args.limit is not None and args.limit < 1):
        parser.error('Delay must be non-negative; page and PDF limits must be positive')
    if args.action == 'sync':
        sync_listings(
            args.data_dir,
            bench_id=args.bench,
            adverse=args.adverse,
            limit_pages=args.max_pages,
            refresh=args.refresh,
            delay=args.delay,
            workers=args.workers,
        )
    elif args.action == 'sync-pdfs':
        sync_pdfs(args.data_dir, limit=args.limit, delay=args.delay, retry_failed=args.retry_failed)
    elif args.action == 'follow-pdfs':
        follow_pdfs(args.data_dir, batch_size=args.limit or 25, delay=args.delay)
    else:
        print(json.dumps(import_status(args.data_dir), indent=2))
if __name__ == '__main__':
    main()
