"""Count matching IBBI listing PDFs without downloading PDFs or calling Codex.

From the project directory: python scripts/count_ibbi_order_remarks.py URL
"""
import argparse
import csv
import json
import sys
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.ibbi_orders import ORDER_REMARK_TERMS, matches_order_remarks, parse_listing


def count_orders(url, max_pages=None, delay=0.5, csv_path=None):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in {'ibbi.gov.in', 'www.ibbi.gov.in'}
            or parts.username or parts.password or parts.port not in {None, 443}
            or not parts.path.startswith('/orders/')):
        raise ValueError('Pass an HTTPS IBBI listing URL, such as https://ibbi.gov.in/orders/nclt')
    query = parse_qs(parts.query, keep_blank_values=True)
    page = int(query.get('page', ['1'])[0])
    if page < 1:
        raise ValueError('Page must be positive')
    result = {'pages_checked': 0, 'total_rows': 0, 'matching_rows': 0,
              'unique_matching_pdfs': 0, 'empty_remarks': 0,
              'rows_by_term': {term: 0 for term in ORDER_REMARK_TERMS}, 'per_page': []}
    matched = set()
    with ExitStack() as stack:
        writer = None
        if csv_path is not None:
            csv_file = stack.enter_context(Path(csv_path).open('w', newline='', encoding='utf-8-sig'))
            columns = ['page', 'url', 'total_pdfs', 'liquidation', 'not_liquidation',
                       'final_order', 'sale_as_going_concern', 'sale_as_a_going_concern',
                       'matching_pdfs', 'nonmatching_pdfs', 'empty_remarks']
            writer = csv.DictWriter(csv_file, fieldnames=columns)
            writer.writeheader()
            csv_file.flush()
            result['csv_path'] = str(Path(csv_path).resolve())
        client = stack.enter_context(httpx.Client(timeout=45, follow_redirects=False,
                      headers={'User-Agent': 'IBC-Remarks-Counter/1.0'}))
        while True:
            query['page'] = [str(page)]
            page_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ''))
            # Only listing URLs are requested. Redirects fail rather than following a PDF.
            response = client.get(page_url)
            response.raise_for_status()
            rows, last, _ = parse_listing(response.text, allow_empty=True)
            result['pages_checked'] += 1
            result['total_rows'] += len(rows)
            counts = dict.fromkeys(ORDER_REMARK_TERMS, 0)
            matching = empty = 0
            for row in rows:
                remarks = ' '.join(row['remarks'].casefold().split())
                result['empty_remarks'] += not bool(remarks)
                empty += not bool(remarks)
                for term in ORDER_REMARK_TERMS:
                    result['rows_by_term'][term] += term in remarks
                    counts[term] += term in remarks
                if matches_order_remarks(row['remarks']):
                    result['matching_rows'] += 1
                    matching += 1
                    matched.add(row['pdf_url'])
            page_counts = {
                'page': page, 'url': page_url, 'total_pdfs': len(rows),
                'liquidation': counts['liquidation'],
                'not_liquidation': len(rows) - counts['liquidation'],
                'final_order': counts['final order'],
                'sale_as_going_concern': counts['sale as going concern'],
                'sale_as_a_going_concern': counts['sale as a going concern'],
                'matching_pdfs': matching, 'nonmatching_pdfs': len(rows) - matching,
                'empty_remarks': empty,
            }
            result['per_page'].append(page_counts)
            if writer is not None:
                writer.writerow(page_counts)
                csv_file.flush()
            print(f'Page {page}/{last}: total={len(rows)}, liquidation={counts["liquidation"]}, '
                  f'not liquidation={page_counts["not_liquidation"]}, final order={counts["final order"]}, '
                  f'sale as going concern={counts["sale as going concern"]}, '
                  f'sale as a going concern={counts["sale as a going concern"]}, '
                  f'matching={matching}, nonmatching={len(rows) - matching}', file=sys.stderr)
            if page >= last or (max_pages is not None and result['pages_checked'] >= max_pages):
                result['complete'] = page >= last
                break
            page += 1
            time.sleep(delay)
    result['unique_matching_pdfs'] = len(matched)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='IBBI listing URL; existing query filters are preserved')
    parser.add_argument('--max-pages', type=int, help='Optional limit; default checks through the last page')
    parser.add_argument('--delay', type=float, default=0.5)
    parser.add_argument('--csv', type=Path, default=Path(
        f'ibbi_order_remarks_{datetime.now():%Y%m%d_%H%M%S_%f}.csv'),
        help='CSV output path; default is a timestamped file in the current directory')
    args = parser.parse_args()
    if args.delay < 0 or (args.max_pages is not None and args.max_pages < 1):
        parser.error('Delay must be non-negative and max-pages must be positive')
    try:
        print(json.dumps(count_orders(args.url, args.max_pages, args.delay, args.csv), indent=2))
    except (ValueError, OSError, httpx.HTTPError) as exc:
        parser.exit(1, f'Count failed: {exc}. Any completed pages remain in {args.csv}; '
                    'the CSV may be partial.\n')


if __name__ == '__main__':
    main()
