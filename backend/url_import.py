"""Bounded public-page crawler. Never follows private addresses or implicit redirects."""
import logging
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import httpx
from bs4 import BeautifulSoup
from . import codex_extraction
from .codex_extraction import final_decision
from .ibbi_orders import parse_listing, matches_order_remarks, safe_pdf_url
from .models import ImportRequest

class UrlImporter:

    @staticmethod
    def public_url(url):
        parts = urlsplit(url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            raise ValueError('Use a public HTTP or HTTPS URL without credentials')
        if parts.port not in {None, 80, 443}:
            raise ValueError('Only standard public HTTP/HTTPS ports are supported')
        try:
            addresses = socket.getaddrinfo(
                parts.hostname,
                parts.port or (443 if parts.scheme == 'https' else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValueError('The public source hostname could not be resolved') from exc
        if not addresses or any((not ipaddress.ip_address(a[4][0]).is_global for a in addresses)):
            raise ValueError('Private, loopback and reserved addresses cannot be imported')
        return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))

    @staticmethod
    def fetch(client, url, limit):
        for redirect in range(6):
            url = public_url(url)
            for attempt in range(3):
                try:
                    with client.stream('GET', url) as response:
                        if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                            time.sleep(attempt + 1)
                            continue
                        if response.is_redirect:
                            location = response.headers.get('location')
                            if not location:
                                raise ValueError('Source returned a redirect without a destination')
                            url = urljoin(url, location)
                            break
                        response.raise_for_status()
                        content = bytearray()
                        for chunk in response.iter_bytes():
                            content.extend(chunk)
                            if len(content) > limit:
                                raise ValueError('Source file exceeds the download size limit')
                        return (bytes(content), url)
                except httpx.TransportError:
                    if attempt == 2:
                        raise ValueError('Source connection failed after three attempts') from None
                    time.sleep(attempt + 1)
            else:
                raise ValueError('Source could not be downloaded')
        raise ValueError('Too many source redirects')

    @staticmethod
    def crawl(request, ingest, progress):
        result = {
            'pages': 0,
            'discovered': 0,
            'imported': 0,
            'duplicates': 0,
            'failed': 0,
            'skipped': 0,
            'review_required': 0,
            'items': [],
            'review_items': [],
            'errors': [],
            'limits_reached': False,
        }
        queue, visited, files = ([request.url], set(), set())
        pending = {}
        result['phase'] = 'discovering'
        result['processed'] = 0
        ibbi_host = urlsplit(request.url).hostname in {'ibbi.gov.in', 'www.ibbi.gov.in'}
        screen_orders = ibbi_host and (urlsplit(request.url).path.startswith('/orders/') or urlsplit(request.url).path.startswith('/uploads/order/'))
        subjects = {}
        if screen_orders and urlsplit(request.url).path.startswith('/uploads/order/'):
            result['errors'].append({'url': request.url, 'error':
                'Use an IBBI listing page URL so Orders Remarks can be checked before downloading PDFs.'})
            return result

        def import_pdf(url, content=None):
            if url in files:
                return
            files.add(url)
            result['current_file'] = urlsplit(url).path.rsplit('/', 1)[-1]
            progress(result)
            logging.getLogger('ibc.import').info('PDF processing %s/%s file=%s', result['processed'] + 1, result['discovered'], result['current_file'])
            try:
                if content is None:
                    content, url = fetch(client, url, 15 * 1024 * 1024)
                prefix = content.find(b'%PDF-', 0, 8192)
                if prefix > 0:
                    content = content[prefix:]
                if screen_orders:
                    extraction = codex_extraction.extract_pdf(content)
                    decision = final_decision(extraction)
                    if decision is None:
                        reason = 'Codex SDK did not identify an operative final order; review the original PDF'
                        result['review_required'] += 1
                        result['review_items'].append(
                            {'url': url, 'subject': subjects.get(url, ''), 'reason': reason},
                        )
                        logging.getLogger('ibc.import').info('PDF needs review file=%s reason=%s', result['current_file'], reason)
                        result['processed'] += 1
                        progress(result)
                        return
                    record = ingest(
                        content,
                        urlsplit(url).path.rsplit('/', 1)[-1] or 'document.pdf',
                        url,
                        preextracted=extraction,
                    )
                else:
                    record = ingest(content, urlsplit(url).path.rsplit('/', 1)[-1] or 'document.pdf', url)
                result['duplicates' if record['duplicate'] else 'imported'] += 1
                result['items'].append(
                    {'url': url, 'removed_prefix_bytes': max(prefix, 0), **record},
                )
            except (ValueError, httpx.HTTPError) as exc:
                result['failed'] += 1
                logging.getLogger('ibc.import').warning('PDF failed file=%s error=%s', result['current_file'], str(exc)[:500])
                result['errors'].append({'url': url, 'error': str(exc)[:500]})
            except Exception as exc:
                from fastapi import HTTPException
                if not isinstance(exc, HTTPException):
                    raise
                result['failed'] += 1
                result['errors'].append({'url': url, 'error': str(exc.detail)[:500]})
            logging.getLogger('ibc.import').info('PDF finished file=%s processed=%s/%s imported=%s duplicates=%s failed=%s', result['current_file'], result['processed'] + 1, result['discovered'], result['imported'], result['duplicates'], result['failed'])
            result['processed'] += 1
            progress(result)
        with httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            trust_env=False,
            headers={'User-Agent': 'IBC-Research-Importer/1.0'},
        ) as client:
            while queue and len(pending) < request.max_files and (request.max_pages is None or result['pages'] < request.max_pages):
                page = queue.pop(0)
                if page in visited:
                    continue
                visited.add(page)
                try:
                    content, final = fetch(client, page, 15 * 1024 * 1024)
                    result['pages'] += 1
                    if content.find(b'%PDF-', 0, 8192) >= 0:
                        pending.setdefault(final, content)
                        result['discovered'] = len(pending)
                        progress(result)
                        continue
                    soup = BeautifulSoup(content, 'html.parser')
                    if screen_orders:
                        rows, _, _ = parse_listing(
                            content.decode('utf-8', errors='replace'), allow_empty=True)
                        subjects.update({item['pdf_url']: item['subject'] for item in rows})
                        allowed_pdfs = {item['pdf_url'] for item in rows if matches_order_remarks(item['remarks'])}
                        result['skipped'] += sum(not matches_order_remarks(item['remarks']) for item in rows)
                    for anchor in soup.select('a[href], iframe[src], embed[src], object[data]'):
                        url = urljoin(
                            final,
                            anchor.get('href') or anchor.get('src') or anchor.get('data'),
                        )
                        parts = urlsplit(url)
                        if parts.scheme not in {'http', 'https'}:
                            continue
                        url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))
                        if parts.path.lower().endswith('.pdf') or anchor.get('type') == 'application/pdf':
                            if screen_orders and (not parts.path.startswith('/uploads/order/')):
                                continue
                            if screen_orders:
                                try:
                                    url = safe_pdf_url(url)
                                except ValueError:
                                    continue
                                if url not in allowed_pdfs:
                                    continue
                            if url not in pending:
                                if len(pending) < request.max_files:
                                    pending[url] = None
                                    result['discovered'] = len(pending)
                                else:
                                    result['limits_reached'] = True
                        elif parts.netloc == urlsplit(final).netloc and ('next' in anchor.get('rel', []) or 'pagination' in str(anchor.parent.get('class', [])) or (parts.path == urlsplit(final).path and 'page=' in parts.query)) and (url not in visited) and (url not in queue):
                            queue.append(url)
                except (ValueError, httpx.HTTPError) as exc:
                    result['errors'].append({'url': page, 'error': str(exc)[:500]})
                progress(result)
            result['phase'] = 'processing'
            logging.getLogger('ibc.import').info('PDF discovery complete: files_found=%s listing_pages=%s remarks_skipped=%s', result['discovered'], result['pages'], result['skipped'])
            progress(result)
            for url, content in pending.items():
                import_pdf(url, content)
            result['current_file'] = None
            result['phase'] = 'finished'
        result['limits_reached'] = result['limits_reached'] or bool(queue) or len(files) >= request.max_files
        if not result['discovered'] and not result['skipped'] and (not result['errors']):
            result['errors'].append(
                {
                    'url': request.url,
                    'error': 'No public PDF links found. The page may require JavaScript, a form submission or login.',
                },
            )
        return result
public_url = UrlImporter.public_url
fetch = UrlImporter.fetch
crawl = UrlImporter.crawl
