"""Walk the public IBBI NCLT listing and write PDFs worth downloading.

Only two Orders Remarks qualify, after lowercasing and collapsing spaces
and hyphens:

- Approval of Resolution Plan
- Liquidation

Admission orders, liquidator appointments, dissolution, closure, and
voluntary liquidation stay off the list. No PDFs are downloaded.

Run from the project root:

    .venv/bin/python -m scripts.nclt_download_list
    .venv/bin/python -m scripts.nclt_download_list --max-pages 2
    .venv/bin/python -m scripts.nclt_download_list --start-page 40
"""
import argparse
import csv
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

BASE = "https://ibbi.gov.in"
LISTING = f"{BASE}/orders/nclt"
USER_AGENT = "IBC-Evidence-Explorer/0.2 (research importer; contact: local operator)"
DOWNLOAD_REMARKS = {
    "approval of resolution plan": "plan",
    "liquidation": "liquidation",
}
LOG = logging.getLogger("nclt_download_list")


def normalize_remark(remark: str) -> str:
    return re.sub(r"\s+", " ", (remark or "").casefold().replace("-", " ")).strip()


def pdf_url(href: str) -> str:
    parsed = urlparse(urljoin(BASE, href))
    if parsed.scheme != "https" or parsed.hostname not in {"ibbi.gov.in", "www.ibbi.gov.in"}:
        raise ValueError("PDF URL must be hosted by IBBI")
    path = re.sub(r"/+", "/", parsed.path)
    if not path.startswith("/uploads/order/") or not path.lower().endswith(".pdf"):
        raise ValueError("PDF URL is outside the IBBI orders directory")
    return parsed._replace(path=path, query="", fragment="").geturl()


def parse_listing(html: str):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.reporttable")
    if table is None:
        raise ValueError("IBBI orders table missing; the page may have changed")
    rows = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td", recursive=False)
        anchor = next((a for a in tr.select("a[href]") if ".pdf" in a["href"].lower()), None)
        if len(cells) < 3 or not anchor:
            continue
        try:
            link = pdf_url(anchor["href"])
        except ValueError:
            continue
        order_date = ""
        for cell in cells:
            text = cell.get_text(" ", strip=True)
            for fmt in ("%d %b, %Y", "%d %B, %Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
                try:
                    order_date = datetime.strptime(text, fmt).date().isoformat()
                    break
                except ValueError:
                    continue
            if order_date:
                break
        subject = re.sub(r"\s*\([\d,.]+\s*(?:KB|MB)\)\s*$", "", anchor.get_text(" ", strip=True), flags=re.I)
        remarks = cells[-1].get_text(" ", strip=True) if not cells[-1].find("a", href=True) else ""
        rows.append({"pdf_url": link, "subject": subject, "order_remarks": remarks, "order_date": order_date})
    last_page = 1
    for anchor in soup.select("ul.pagination a[href]"):
        values = parse_qs(urlparse(anchor["href"]).query).get("page", [])
        if values and values[0].isdigit():
            last_page = max(last_page, int(values[0]))
    return rows, last_page


def fetch(client: httpx.Client, page: int) -> str:
    url = LISTING + "?" + urlencode({"page": page})
    for attempt in range(5):
        try:
            response = client.get(url)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise httpx.HTTPStatusError("temporary IBBI response", request=response.request, response=response)
            response.raise_for_status()
            if urlparse(str(response.url)).hostname not in {"ibbi.gov.in", "www.ibbi.gov.in"}:
                raise ValueError("Unexpected redirect outside IBBI")
            return response.text
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {429, 500, 502, 503, 504}:
                raise
            if attempt == 4:
                LOG.error("Page %s failed after 5 attempts: %s", page, exc)
                raise
            wait = min(2 ** attempt, 8)
            LOG.warning("Page %s attempt %s failed (%s). Retrying in %ss.", page, attempt + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"IBBI page {page} could not be fetched")


def configure_logging(log_path: Path):
    LOG.setLevel(logging.INFO)
    LOG.handlers.clear()
    LOG.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")):
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def remark_kind(remark: str) -> str | None:
    return DOWNLOAD_REMARKS.get(normalize_remark(remark))


def main():
    parser = argparse.ArgumentParser(description="CSV of NCLT plan-approval and liquidation orders")
    parser.add_argument("--output", type=Path, default=Path("data/nclt-download.csv"))
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, help="Stop after this many listing pages")
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()
    if args.start_page < 1 or (args.max_pages is not None and args.max_pages < 1) or args.delay < 0:
        parser.error("start page and max pages must be positive; delay must be non-negative")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    log_path = args.output.with_suffix(".log")
    configure_logging(log_path)
    fields = ["pdf_url", "subject", "order_remarks", "order_date"]
    resume = args.start_page > 1 and args.output.exists()
    seen = set()
    counts = {"plan": 0, "liquidation": 0}
    if resume:
        with args.output.open(newline="", encoding="utf-8") as existing:
            for row in csv.DictReader(existing):
                seen.add(row["pdf_url"])
                kind = remark_kind(row["order_remarks"])
                if kind:
                    counts[kind] += 1
    written = len(seen)
    LOG.info("Log file: %s", log_path)
    if resume:
        LOG.info("Resuming at page %s. CSV already has %s rows (%s plan approvals, %s liquidations).",
                 args.start_page, written, counts["plan"], counts["liquidation"])
    else:
        LOG.info("Starting at page %s. Writing %s.", args.start_page, args.output)
    started = time.monotonic()
    with args.output.open("a" if resume else "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not resume:
            writer.writeheader()
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=45) as client:
            first_html = fetch(client, 1)
            _, end = parse_listing(first_html)
            target = end if args.max_pages is None else min(end, args.start_page + args.max_pages - 1)
            LOG.info("NCLT listing has %s pages. This run scans pages %s-%s.", end, args.start_page, target)
            page = args.start_page
            fetched = 0
            while page <= end:
                try:
                    html = first_html if page == 1 else fetch(client, page)
                    rows, current_last = parse_listing(html)
                except (httpx.HTTPError, ValueError, OSError):
                    LOG.exception("Stopped on page %s. Resume with --start-page %s", page, page)
                    raise
                if not args.max_pages and current_last > end:
                    end = current_last
                    LOG.info("Listing length is now %s pages.", end)
                added = {"plan": 0, "liquidation": 0}
                duplicates = 0
                for row in rows:
                    kind = remark_kind(row["order_remarks"])
                    if not kind:
                        continue
                    if row["pdf_url"] in seen:
                        duplicates += 1
                        continue
                    seen.add(row["pdf_url"])
                    writer.writerow(row)
                    counts[kind] += 1
                    added[kind] += 1
                    LOG.info("  %s %s | %s", row["order_date"] or "undated", row["order_remarks"], row["subject"])
                kept = added["plan"] + added["liquidation"]
                written += kept
                fetched += 1
                handle.flush()
                elapsed = time.monotonic() - started
                target = end if args.max_pages is None else min(end, args.start_page + args.max_pages - 1)
                span = max(target - args.start_page + 1, 1)
                remaining = max(target - page, 0)
                eta = format_duration(remaining * elapsed / fetched) if fetched else "unknown"
                LOG.info(
                    "Page %s/%s (%.1f%% of this run) listed %s, kept %s (%s plan, %s liquidation), skipped %s, duplicates %s. "
                    "CSV %s (%s plan, %s liquidation). Elapsed %s, about %s left.",
                    page, target, 100 * fetched / span,
                    len(rows), kept, added["plan"], added["liquidation"], len(rows) - kept - duplicates, duplicates,
                    written, counts["plan"], counts["liquidation"], format_duration(elapsed), eta,
                )
                if args.max_pages and fetched >= args.max_pages:
                    break
                page += 1
                if page <= end and args.delay:
                    time.sleep(args.delay)
    LOG.info("Finished. %s rows in %s (%s plan approvals, %s liquidations) after %s.",
             written, args.output, counts["plan"], counts["liquidation"], format_duration(time.monotonic() - started))


if __name__ == "__main__":
    main()
