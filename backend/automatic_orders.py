from .config import DEFAULT_CATEGORIES
'Persistent IBBI collection with per-source checkpoints and a single scheduler lease.'
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from urllib.parse import urljoin, urlsplit, urlencode
from uuid import uuid4
import httpx
from bs4 import BeautifulSoup
from .ibbi_orders import BASE, parse_listing, matches_order_remarks
from . import codex_extraction
from .codex_extraction import SCREENING_VERSION, final_decision
from .url_import import fetch

def stamp():
    return datetime.now(timezone.utc).isoformat()

class AutomaticOrders:

    def __init__(self, connect, ingest):
        self.connect, self.ingest = (connect, ingest)
        self.stop_event = Event()
        self.thread = None
        self.manual_thread = None

    def start(self):
        self.thread = Thread(target=self.loop, name='ibbi-scheduler', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=40)
        if self.manual_thread:
            self.manual_thread.join(timeout=40)

    def status(self):
        with self.connect() as db:
            settings = dict(db.scheduler_settings())
            sources = [dict(r) for r in db.all_ibbi_sources()]
        return {
            **settings,
            'sources': sources,
            'running': bool(settings['lease_until'] and settings['lease_until'] > stamp()),
        }

    def configure(self, settings):
        self.validate_category(settings.category)
        with self.connect() as db:
            db.configure_scheduler(
                int(settings.enabled),
                settings.interval_minutes,
                stamp() if settings.enabled else None,
                settings.category,
            )
        return self.status()

    def validate_category(self, category):
        if category == 'all':
            return
        with self.connect() as db:
            if not db.ibbi_category(category):
                raise ValueError('Unknown IBBI order category')

    def claim(self, due_only=False, category=None):
        current = stamp()
        until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        job_id = uuid4().hex
        with self.connect() as db:
            old_job = db.scheduler_active_job()[0]
            if db.claim_scheduler_lease(until, job_id, current, due_only) != 1:
                return None
            if old_job:
                db.interrupt_import_job(old_job)
            selected = category or db.scheduler_category()[0]
            db.insert_import_job(
                job_id,
                f'{BASE}/orders ({selected} automatic sync)',
                'queued',
                current,
                current,
                json.dumps({'category': selected}),
            )
        return job_id

    def trigger(self, category=None):
        if category is not None:
            self.validate_category(category)
        with self.connect() as db:
            row = db.scheduler_lease()
            if row['lease_until'] and row['lease_until'] > stamp():
                return False
            db.schedule_next_run(stamp())
        job_id = self.claim(category=category)
        if not job_id:
            return False
        self.manual_thread = Thread(target=self.run, args=(job_id,), name='ibbi-sync', daemon=True)
        self.manual_thread.start()
        return job_id

    def loop(self):
        while not self.stop_event.is_set():
            try:
                job_id = self.claim(due_only=True)
                if job_id:
                    self.run(job_id)
            except Exception:
                logging.exception('IBBI scheduler check failed')
            self.stop_event.wait(5)

    def progress(self, job_id, result, status='running'):
        with self.connect() as db:
            db.update_import_progress(status, json.dumps(result), stamp(), job_id)
            db.persist_import_files(job_id, result, stamp())
            lease = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            db.renew_scheduler_lease(lease, job_id)

    def discover(self, db, html):
        for anchor in BeautifulSoup(html, 'html.parser').select('a[href]'):
            url = urljoin(BASE, anchor['href'])
            parts = urlsplit(url)
            if parts.hostname in {'ibbi.gov.in', 'www.ibbi.gov.in'} and parts.path.startswith('/orders/'):
                category = parts.path.removeprefix('/orders/').strip('/')
                if category and '/' not in category:
                    db.register_ibbi_source(category, f'{BASE}/orders/{category}', stamp())

    def run(self, job_id):
        with self.connect() as db:
            selected = json.loads(db.import_job_result(job_id)[0]).get('category', 'all')
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
            'known_orders': 0,
            'category': selected,
        }
        heartbeat_stop = Event()

        def heartbeat():
            while not heartbeat_stop.wait(30):
                try:
                    with self.connect() as db:
                        until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                        db.release_scheduler_lease(until, job_id)
                except Exception:
                    logging.exception('Scheduler heartbeat failed')
        heartbeat_thread = Thread(target=heartbeat, name='ibbi-heartbeat', daemon=True)
        heartbeat_thread.start()
        try:
            self.progress(job_id, result)
            with httpx.Client(
                timeout=30,
                follow_redirects=False,
                trust_env=False,
                headers={'User-Agent': 'IBC-Research-Collector/1.0'},
            ) as client:
                try:
                    content, _ = fetch(client, f'{BASE}/orders/nclt', 15 * 1024 * 1024)
                    with self.connect() as db:
                        self.discover(db, content.decode('utf-8', errors='replace'))
                except (ValueError, httpx.HTTPError) as exc:
                    result['errors'].append({'url': f'{BASE}/orders/nclt', 'error': str(exc)[:500]})
                with self.connect() as db:
                    sources = [dict(r) for r in db.selected_ibbi_sources(selected)]
                for source in sources:
                    if self.stop_event.is_set():
                        break
                    self.collect_source(client, source, job_id, result)
            status = 'interrupted' if self.stop_event.is_set() else 'completed_with_errors' if result['errors'] else 'completed'
            result['current_file'] = None
            logging.getLogger('ibc.import').info('Scheduled import finished job=%s found=%s imported=%s duplicates=%s review=%s failed=%s', job_id, result['discovered'], result['imported'], result['duplicates'], result['review_required'], result['failed'])
            self.progress(job_id, result, status)
        except Exception:
            logging.exception('Automatic IBBI sync failed')
            result['error'] = 'Collector stopped unexpectedly; saved PDFs and checkpoints are retained.'
            self.progress(job_id, result, 'failed')
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=10)
            with self.connect() as db:
                interval = db.scheduler_interval()[0]
                next_run = (datetime.now(timezone.utc) + timedelta(minutes=interval)).isoformat()
                db.release_scheduler_lease(next_run, job_id)

    def collect_source(self, client, source, job_id, result):
        started, had_errors = (stamp(), False)
        page = source['next_page'] if not source['last_sync'] else 1
        last = page
        with self.connect() as db:
            db.start_source_sync(started, source['category'])
        try:
            while page <= last and (not self.stop_event.is_set()):
                url = source['url'] + '?' + urlencode({'page': page})
                content, _ = fetch(client, url, 15 * 1024 * 1024)
                rows, last, benches = parse_listing(content.decode('utf-8', errors='replace'), allow_empty=True)
                with self.connect() as db:
                    for bench_id, label in benches.items():
                        db.upsert_source_filter(bench_id, label, started, stamp())
                result['pages'] += 1
                for order in rows:
                    if self.stop_event.is_set():
                        break
                    with self.connect() as db:
                        existing = db.source_order_import_state(order['pdf_url'])
                        db.upsert_source_order(
                            order['id'],
                            order['pdf_url'],
                            order['order_date'],
                            order['subject'],
                            order['remarks'],
                            started,
                            stamp(),
                        )
                        db.add_order_category(order['id'], source['category'])
                    if not matches_order_remarks(order['remarks']):
                        with self.connect() as db:
                            db.execute("UPDATE source_orders SET pdf_status='skipped_nonfinal', pdf_error=? "
                                       "WHERE id=? AND document_id IS NULL",
                                       ('Orders Remarks do not match the configured import terms', order['id']))
                        result['skipped'] += 1
                        continue
                    if existing and existing['document_id'] and existing['case_id'] and (existing['pdf_status'] == 'downloaded') and (existing['subject'] == order['subject']) and (existing['order_date'] == order['order_date']):
                        with self.connect() as db:
                            db.set_document_category(existing['document_id'], source['category'])
                        result['known_orders'] += 1
                        continue
                    if existing and existing['pdf_status'] == 'review_required' and (existing['subject'] == order['subject']) and (existing['remarks'] == order['remarks']) and (existing['order_date'] == order['order_date']) and (existing['pdf_error'] or '').startswith(SCREENING_VERSION + ':'):
                        result['known_orders'] += 1
                        continue
                    result['current_file'] = urlsplit(order['pdf_url']).path.rsplit('/', 1)[-1]
                    logging.getLogger('ibc.import').info('Scheduled PDF processing job=%s file=%s subject=%s', job_id, result['current_file'], order['subject'])
                    result['discovered'] += 1
                    self.progress(job_id, result)
                    result.setdefault('since_last_sync', {})[source['category']] = source['last_sync']
                    try:
                        pdf, _ = fetch(client, order['pdf_url'], 50 * 1024 * 1024)
                        prefix = pdf.find(b'%PDF-', 0, 8192)
                        if prefix > 0:
                            pdf = pdf[prefix:]
                        digest = hashlib.sha256(pdf).hexdigest()
                        with self.connect() as db:
                            stored_pdf = db.stored_document_extraction(digest)
                        extraction = json.loads(stored_pdf['extraction']) if stored_pdf else codex_extraction.extract_pdf(pdf)
                        decision = final_decision(extraction)
                        if decision is None:
                            reason = 'Codex SDK did not identify an operative final order; review the original PDF'
                            with self.connect() as db:
                                db.mark_order_for_review(
                                    f'{SCREENING_VERSION}: {reason}',
                                    order['id'],
                                )
                            result['review_required'] += 1
                            result['review_items'].append(
                                {
                                    'url': order['pdf_url'],
                                    'subject': order['subject'],
                                    'reason': reason,
                                },
                            )
                            result['review_items'] = result['review_items'][-100:]
                            self.progress(job_id, result)
                            continue
                        item = self.ingest(
                            pdf,
                            urlsplit(order['pdf_url']).path.rsplit('/', 1)[-1],
                            order['pdf_url'],
                            extraction,
                            decision,
                        )
                        result['duplicates' if item['duplicate'] else 'imported'] += 1
                        result['items'].append(
                            {'url': order['pdf_url'], 'category': source['category'], **item},
                        )
                        result['items'] = result['items'][-20:]
                        with self.connect() as db:
                            db.set_document_category(item['document_id'], source['category'])
                            stored = db.document_hash(item['document_id'])
                            if not stored or stored['sha256'] != digest:
                                raise ValueError('Imported PDF hash does not match this source row')
                            db.complete_order_import(
                                item['case_id'],
                                item['document_id'],
                                digest,
                                len(pdf),
                                stamp(),
                                order['id'],
                            )
                    except Exception as exc:
                        from fastapi import HTTPException
                        if not isinstance(exc, (HTTPException, ValueError, httpx.HTTPError)):
                            raise
                        had_errors = True
                        error = str(exc.detail if isinstance(exc, HTTPException) else exc)[:500]
                        result['failed'] += 1
                        result['errors'].append({'url': order['pdf_url'], 'error': error})
                        with self.connect() as db:
                            db.fail_order_import(error, order['id'])
                    self.progress(job_id, result)
                    self.stop_event.wait(0.3)
                if self.stop_event.is_set():
                    break
                with self.connect() as db:
                    db.save_source_page(
                        'category:' + source['category'],
                        page,
                        stamp(),
                        len(rows),
                        last,
                    )
                    db.clear_source_page_failure('category:' + source['category'], page)
                    db.advance_source_page(page + 1, source['category'])
                page += 1
                self.progress(job_id, result)
                self.stop_event.wait(0.5)
            with self.connect() as db:
                pending = db.pending_source_order_count(source['category'])[0]
                if pending and (not self.stop_event.is_set()):
                    had_errors = True
                    result['errors'].append(
                        {
                            'url': source['url'],
                            'error': f'{pending} order PDFs remain unstored; the next sync will retry them.',
                        },
                    )
                if not self.stop_event.is_set() and (not had_errors):
                    db.complete_source_sync(started, source['category'])
                else:
                    db.stop_source_sync(
                        'interrupted' if self.stop_event.is_set() else 'completed_with_errors',
                        source['category'],
                    )
        except (ValueError, httpx.HTTPError) as exc:
            result['errors'].append({'url': source['url'], 'error': str(exc)[:500]})
            with self.connect() as db:
                db.fail_source_sync(str(exc)[:500], source['category'])
                db.record_source_page_failure(
                    'category:' + source['category'],
                    page,
                    str(exc)[:500],
                    stamp(),
                )
            self.progress(job_id, result)
from .models import ScheduleSettings
