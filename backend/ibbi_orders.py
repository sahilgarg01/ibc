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

BASE = "https://ibbi.gov.in"
LISTING = f"{BASE}/orders/nclt"
USER_AGENT = "IBC-Evidence-Explorer/0.2 (research importer; contact: local operator)"
MAX_PDF_BYTES = 50 * 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_pdf_url(url: str) -> str:
    parsed = urlparse(urljoin(BASE, url))
    if parsed.scheme != "https" or parsed.hostname not in {"ibbi.gov.in", "www.ibbi.gov.in"}:
        raise ValueError("PDF URL must be hosted by IBBI")
    path = re.sub(r'/+', '/', parsed.path)
    if not path.startswith("/uploads/order/") or not path.lower().endswith(".pdf"):
        raise ValueError("PDF URL is outside the IBBI orders directory")
    return parsed._replace(path=path, query="", fragment="").geturl()


def parse_listing(html: str, *, allow_empty=False):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.reporttable")
    if table is None:
        raise ValueError("IBBI orders table missing; the page may have changed")
    rows = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 3:
            continue
        anchor = next((a for a in tr.select('a[href]') if '.pdf' in a['href'].lower()),None)
        if not anchor:
            continue
        try:
            pdf_url = safe_pdf_url(anchor["href"])
        except ValueError:
            continue
        order_date = None
        for cell in cells:
            date_text = cell.get_text(' ',strip=True)
            for fmt in ('%d %b, %Y','%d %B, %Y','%d-%m-%Y','%d/%m/%Y','%Y-%m-%d'):
                try:
                    order_date = datetime.strptime(date_text,fmt).date().isoformat()
                    break
                except ValueError:
                    continue
            if order_date:
                break
        subject = re.sub(r"\s*\([\d,.]+\s*(?:KB|MB)\)\s*$", "", anchor.get_text(" ", strip=True), flags=re.I)
        rows.append({"id": hashlib.sha256(pdf_url.encode()).hexdigest()[:24],
                     "pdf_url": pdf_url, "order_date": order_date,
                     "subject": subject, "remarks": cells[-1].get_text(" ", strip=True) if not cells[-1].find('a',href=True) else ''})
    if not rows and not allow_empty:
        raise ValueError("No PDF order rows found; refusing to record an empty page")
    last_page = 1
    for a in soup.select("ul.pagination a[href]"):
        values = parse_qs(urlparse(a["href"]).query).get("page", [])
        if values and values[0].isdigit():
            last_page = max(last_page, int(values[0]))
    benches = {option.get("value"): option.get_text(" ", strip=True)
               for option in soup.select('select[name="nclt"] option') if option.get("value")}
    return rows, last_page, benches


def fetch(client: httpx.Client, url: str, *, stream_limit: int | None = None):
    for attempt in range(5):
        try:
            with client.stream("GET", url) as response:
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise httpx.HTTPStatusError("temporary IBBI response", request=response.request, response=response)
                response.raise_for_status()
                if urlparse(str(response.url)).hostname not in {"ibbi.gov.in", "www.ibbi.gov.in"}:
                    raise ValueError("Unexpected redirect outside IBBI")
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if stream_limit is not None and total > stream_limit:
                        raise ValueError(f"PDF exceeds {stream_limit} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {429, 500, 502, 503, 504}:
                raise
            if attempt == 4:
                raise
            time.sleep(min(2 ** attempt, 8))


def listing_url(page: int, bench_id: str | None = None, adverse: str | None = None):
    query = {"page": page}
    if bench_id:
        query["nclt"] = bench_id
    if adverse:
        query["adverse_against"] = adverse
    return LISTING + "?" + urlencode(query)


def sync_listings(root: Path, *, bench_id: str | None = None, adverse: str | None = None,
                  limit_pages: int | None = None,
                  refresh: bool = False, delay: float = 0.5, workers: int = 3,
                  client: httpx.Client | None = None):
    initialize(root)
    if adverse not in {None, "ip", "other"}:
        raise ValueError("Adverse filter must be ip or other")
    key = ":".join(part for part in (f"bench:{bench_id}" if bench_id else None,
                                   f"adverse:{adverse}" if adverse else None) if part) or "all"
    owned = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=45)
    fetched = 0
    try:
        # A fresh first page provides current pagination and filter labels.
        first_html = fetch(client, listing_url(1, bench_id, adverse)).decode("utf-8", errors="replace")
        first_rows, total_pages, benches = parse_listing(first_html)
        if bench_id and bench_id not in benches:
            raise ValueError(f"Unknown IBBI bench filter {bench_id}")
        with connection(root) as db:
            stamp = utc_now()
            for option_id, name in benches.items():
                db.execute("""INSERT INTO source_filters(key,label,first_seen_at,last_seen_at)
                    VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                    label=excluded.label,last_seen_at=excluded.last_seen_at""", (option_id, name, stamp, stamp))
        end = min(total_pages, limit_pages) if limit_pages else total_pages
        with connection(root) as db:
            saved_pages = {r["page_number"] for r in db.execute("SELECT page_number FROM source_pages WHERE filter_key=?", (key,))}
        pending = [p for p in range(2, end + 1) if refresh or p not in saved_pages]

        def load_page(page):
            data = fetch(client, listing_url(page, bench_id, adverse))
            if delay:
                time.sleep(delay)
            rows, current_last, _ = parse_listing(data.decode("utf-8", errors="replace"))
            return page, rows, current_last

        def save_page(page, rows, current_last):
            nonlocal fetched
            stamp = utc_now()
            with connection(root) as db:
                db.execute("BEGIN IMMEDIATE")
                for row in rows:
                    db.execute("""INSERT INTO source_orders
                        (id,pdf_url,order_date,subject,remarks,bench_id,bench_name,first_seen_at,last_seen_at)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(pdf_url) DO UPDATE SET
                        order_date=excluded.order_date,subject=excluded.subject,
                        remarks=excluded.remarks,last_seen_at=excluded.last_seen_at,
                        bench_id=COALESCE(excluded.bench_id,source_orders.bench_id),
                        bench_name=COALESCE(excluded.bench_name,source_orders.bench_name)""",
                        (row["id"], row["pdf_url"], row["order_date"], row["subject"], row["remarks"],
                         bench_id, benches.get(bench_id) if bench_id else None, stamp, stamp))
                    if adverse:
                        db.execute("INSERT OR IGNORE INTO source_order_filters(order_id,filter_key) VALUES(?,?)",
                                   (row["id"], f"adverse:{adverse}"))
                db.execute("""INSERT INTO source_pages(filter_key,page_number,fetched_at,row_count,last_page)
                    VALUES(?,?,?,?,?) ON CONFLICT(filter_key,page_number) DO UPDATE SET
                    fetched_at=excluded.fetched_at,row_count=excluded.row_count,last_page=excluded.last_page""",
                    (key, page, stamp, len(rows), current_last))
                db.execute("DELETE FROM source_page_failures WHERE filter_key=? AND page_number=?", (key, page))
            fetched += 1
            if page == 1 or fetched % 25 == 0 or page == end:
                print(f"{key}: page {page}/{total_pages}, fetched this run {fetched}, rows {len(rows)}", flush=True)

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
                        db.execute("""INSERT INTO source_page_failures
                            (filter_key,page_number,last_error,last_attempt_at) VALUES(?,?,?,?)
                            ON CONFLICT(filter_key,page_number) DO UPDATE SET
                            last_error=excluded.last_error,last_attempt_at=excluded.last_attempt_at,
                            attempts=source_page_failures.attempts+1""",
                            (key, page, str(exc)[:1000], utc_now()))
                    print(f"{key}: page {page} failed: {exc}", flush=True)
    finally:
        if owned:
            client.close()
    return {"filter": key, "pages_fetched": fetched, "last_page": total_pages,
            "benches": benches if not bench_id else None}


def download_order(root: Path, order_id: str, client: httpx.Client | None = None):
    initialize(root)
    with connection(root) as db:
        row = db.execute("SELECT * FROM source_orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            raise ValueError("Order not found")
        if row["pdf_status"] == "downloaded" and (root / "source_documents" / f"{order_id}.pdf").exists():
            return root / "source_documents" / f"{order_id}.pdf"
        url = safe_pdf_url(row["pdf_url"])
    owned = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=90)
    try:
        raw = fetch(client, url, stream_limit=MAX_PDF_BYTES + 4096)
        prefix = raw.find(b"%PDF-", 0, 4096)
        if prefix < 0:
            raise ValueError("IBBI response contains no PDF header")
        content = raw[prefix:]
        folder = root / "source_documents"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{order_id}.pdf"
        part = folder / f"{order_id}.part"
        part.write_bytes(content)
        part.replace(path)
        with connection(root) as db:
            db.execute("""UPDATE source_orders SET pdf_status='downloaded',pdf_error=NULL,
                pdf_sha256=?,pdf_bytes=?,pdf_prefix_bytes=?,pdf_fetched_at=? WHERE id=?""",
                (hashlib.sha256(content).hexdigest(), len(content), prefix, utc_now(), order_id))
        return path
    finally:
        if owned:
            client.close()


def sync_pdfs(root: Path, *, limit: int | None = None, delay: float = 0.5,
              retry_failed: bool = False):
    initialize(root)
    with connection(root) as db:
        status_filter = "pdf_status IN ('pending','failed')" if retry_failed else "pdf_status='pending'"
        sql = f"SELECT id FROM source_orders WHERE {status_filter} ORDER BY order_date DESC LIMIT ?"
        ids = [r["id"] for r in db.execute(sql, (limit or 1000000,))]
    success = failed = 0
    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=90) as client:
        for index, order_id in enumerate(ids, 1):
            if shutil.disk_usage(root).free < 5 * 1024 ** 3:
                print("Stopping PDF downloads: less than 5 GB of free space remains", flush=True)
                break
            try:
                download_order(root, order_id, client)
                success += 1
            except (httpx.HTTPError, ValueError, OSError) as exc:
                failed += 1
                with connection(root) as db:
                    db.execute("UPDATE source_orders SET pdf_status='failed',pdf_error=? WHERE id=?", (str(exc)[:1000], order_id))
                print(f"PDF failed {order_id}: {exc}", flush=True)
            if index % 25 == 0 or index == len(ids):
                print(f"PDFs {index}/{len(ids)}: downloaded {success}, failed {failed}", flush=True)
            if index < len(ids):
                time.sleep(delay)
    return {"downloaded": success, "failed": failed}


def follow_pdfs(root: Path, *, batch_size: int = 25, delay: float = 0.5):
    """Keep downloading newly discovered orders until the global listing is complete."""
    initialize(root)
    while True:
        with connection(root) as db:
            pending = db.execute("SELECT COUNT(*) FROM source_orders WHERE pdf_status='pending'").fetchone()[0]
            page_status = db.execute("SELECT COUNT(*),MAX(last_page) FROM source_pages WHERE filter_key='all'").fetchone()
        if pending:
            result = sync_pdfs(root, limit=batch_size, delay=delay)
            if not result["downloaded"] and not result["failed"]:
                break
        elif page_status[1] and page_status[0] >= page_status[1]:
            print("Global listing and PDF queue complete", flush=True)
            break
        else:
            time.sleep(30)


def import_status(root: Path):
    initialize(root)
    with connection(root) as db:
        orders = db.execute("""SELECT COUNT(*) total,
            COALESCE(SUM(CASE WHEN pdf_status='downloaded' THEN 1 ELSE 0 END),0) downloaded,
            COALESCE(SUM(CASE WHEN pdf_status='failed' THEN 1 ELSE 0 END),0) failed,
            COALESCE(SUM(CASE WHEN case_id IS NOT NULL THEN 1 ELSE 0 END),0) reviewed FROM source_orders""").fetchone()
        pages = db.execute("SELECT COUNT(*) done,MAX(last_page) expected FROM source_pages WHERE filter_key='all'").fetchone()
        failed_pages = db.execute("SELECT COUNT(*) FROM source_page_failures WHERE filter_key='all'").fetchone()[0]
        benches = db.execute("SELECT COUNT(DISTINCT filter_key) FROM source_pages WHERE filter_key LIKE 'bench:%%'").fetchone()[0]
    return {"orders": orders["total"], "listing_pages": pages["done"],
            "listing_pages_reported": pages["expected"], "listing_pages_failed": failed_pages,
            "benches_synced": benches,
            "pdfs_downloaded": orders["downloaded"], "pdfs_failed": orders["failed"],
            "orders_in_review": orders["reviewed"]}


def main():
    parser = argparse.ArgumentParser(description="IBBI NCLT public orders importer")
    parser.add_argument("action", choices=["sync", "sync-pdfs", "follow-pdfs", "status"])
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--bench", help="IBBI NCLT filter ID, e.g. 38 for Mumbai")
    parser.add_argument("--adverse", choices=["ip", "other"], help="Site radio filter: adverse against IP or other")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--limit", type=int, help="Maximum PDFs for sync-pdfs")
    parser.add_argument("--refresh", action="store_true", help="Refetch previously saved listing pages")
    parser.add_argument("--retry-failed", action="store_true", help="Retry PDFs whose earlier download failed")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--workers", type=int, default=3, help="Concurrent listing requests (default 3)")
    args = parser.parse_args()
    if args.delay < 0 or args.workers < 1 or args.workers > 5 or (args.max_pages is not None and args.max_pages < 1) or (args.limit is not None and args.limit < 1):
        parser.error("Delay must be non-negative; page and PDF limits must be positive")
    if args.action == "sync":
        sync_listings(args.data_dir, bench_id=args.bench, adverse=args.adverse, limit_pages=args.max_pages,
                      refresh=args.refresh, delay=args.delay, workers=args.workers)
    elif args.action == "sync-pdfs":
        sync_pdfs(args.data_dir, limit=args.limit, delay=args.delay, retry_failed=args.retry_failed)
    elif args.action == "follow-pdfs":
        follow_pdfs(args.data_dir, batch_size=args.limit or 25, delay=args.delay)
    else:
        print(json.dumps(import_status(args.data_dir), indent=2))


if __name__ == "__main__":
    main()
