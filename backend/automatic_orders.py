"""Persistent IBBI collection with per-source checkpoints and a single scheduler lease."""
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from urllib.parse import urljoin, urlsplit, urlencode
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from .ibbi_orders import BASE, parse_listing
from .structured import persist_import_files
from .url_import import fetch

DEFAULT_CATEGORIES = ('supreme-court', 'high-courts', 'nclat', 'nclt', 'drat', 'drts',
                      'ibbi', 'ipa-rvo', 'other-courts')
SCHEMA = """
CREATE TABLE IF NOT EXISTS ibbi_sources (
 category TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, created_at TEXT, last_sync TEXT,
 last_attempt TEXT, next_page INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'pending', error TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_settings (
 id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
 interval_minutes INTEGER NOT NULL DEFAULT 1440, next_run TEXT,
 lease_until TEXT, active_job TEXT
);
CREATE TABLE IF NOT EXISTS order_categories (
 order_id TEXT NOT NULL REFERENCES source_orders(id), category TEXT NOT NULL,
 PRIMARY KEY(order_id,category)
);
CREATE TABLE IF NOT EXISTS document_categories (
 document_id TEXT NOT NULL REFERENCES documents(id), category TEXT NOT NULL,
 PRIMARY KEY(document_id,category)
);
"""


def stamp():
    return datetime.now(timezone.utc).isoformat()


def initialize_automatic(db):
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    if hasattr(db,'raw'):
        db.execute("ALTER TABLE scheduler_settings ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'all'")
        db.execute("ALTER TABLE ibbi_sources ADD COLUMN IF NOT EXISTS created_at TEXT")
    elif 'category' not in {r['name'] for r in db.execute('PRAGMA table_info(scheduler_settings)')}:
        db.execute("ALTER TABLE scheduler_settings ADD COLUMN category TEXT NOT NULL DEFAULT 'all'")
    if not hasattr(db,'raw') and 'created_at' not in {r['name'] for r in db.execute('PRAGMA table_info(ibbi_sources)')}:
        db.execute('ALTER TABLE ibbi_sources ADD COLUMN created_at TEXT')
    db.execute('INSERT OR IGNORE INTO scheduler_settings(id) VALUES(1)')
    for category in DEFAULT_CATEGORIES:
        db.execute('INSERT OR IGNORE INTO ibbi_sources(category,url,created_at) VALUES(?,?,?)',
                   (category, f'{BASE}/orders/{category}', stamp()))
    for row in db.execute('''SELECT DISTINCT d.id,c.category FROM documents d
        JOIN source_orders o ON o.pdf_url=d.source_url JOIN order_categories c ON c.order_id=o.id
        UNION SELECT DISTINCT f.document_id,c.category FROM import_files f
        JOIN source_orders o ON o.pdf_url=f.source_url JOIN order_categories c ON c.order_id=o.id''').fetchall():
        set_document_category(db,row[0],row[1])


def set_document_category(db, document_id, category):
    db.execute('INSERT OR IGNORE INTO document_categories VALUES(?,?)',(document_id,category))
    db.execute("UPDATE documents SET order_kind=? WHERE id=? AND (order_kind IS NULL OR order_kind='unknown')",(category,document_id))


class ScheduleSettings(BaseModel):
    enabled: bool
    interval_minutes: int = Field(default=1440, ge=15, le=10080)
    category: str = Field(default='all',max_length=100)


class AutomaticOrders:
    def __init__(self, connect, ingest):
        self.connect, self.ingest = connect, ingest
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
            settings = dict(db.execute('SELECT * FROM scheduler_settings WHERE id=1').fetchone())
            sources = [dict(r) for r in db.execute('SELECT * FROM ibbi_sources ORDER BY category')]
        return {**settings, 'sources': sources, 'running': bool(settings['lease_until'] and settings['lease_until'] > stamp())}

    def configure(self, settings):
        self.validate_category(settings.category)
        with self.connect() as db:
            db.execute('UPDATE scheduler_settings SET enabled=?,interval_minutes=?,next_run=?,category=? WHERE id=1',
                       (int(settings.enabled), settings.interval_minutes, stamp() if settings.enabled else None,settings.category))
        return self.status()

    def validate_category(self,category):
        if category=='all': return
        with self.connect() as db:
            if not db.execute('SELECT category FROM ibbi_sources WHERE category=?',(category,)).fetchone():
                raise ValueError('Unknown IBBI order category')

    def claim(self, due_only=False, category=None):
        current = stamp()
        until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        job_id = uuid4().hex
        with self.connect() as db:
            old_job = db.execute('SELECT active_job FROM scheduler_settings WHERE id=1').fetchone()[0]
            query = 'UPDATE scheduler_settings SET lease_until=?,active_job=? WHERE id=1 AND (lease_until IS NULL OR lease_until<?)'
            args = [until, job_id, current]
            if due_only:
                query += ' AND enabled=1 AND (next_run IS NULL OR next_run<=?)'
                args.append(current)
            if db.execute(query, args).rowcount != 1:
                return None
            if old_job:
                db.execute("UPDATE import_jobs SET status='interrupted' WHERE id=? AND status IN ('queued','running')",(old_job,))
            selected=category or db.execute('SELECT category FROM scheduler_settings WHERE id=1').fetchone()[0]
            db.execute('INSERT INTO import_jobs VALUES(?,?,?,?,?,?)',
                       (job_id, f'{BASE}/orders ({selected} automatic sync)', 'queued', current, current, json.dumps({'category':selected})))
        return job_id

    def trigger(self,category=None):
        if category is not None: self.validate_category(category)
        # Wake the scheduler without a second worker or a race with a scheduled run.
        with self.connect() as db:
            row = db.execute('SELECT lease_until FROM scheduler_settings WHERE id=1').fetchone()
            if row['lease_until'] and row['lease_until'] > stamp():
                return False
            db.execute('UPDATE scheduler_settings SET next_run=? WHERE id=1', (stamp(),))
        # Explicit manual sync runs even while the recurring schedule is disabled.
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
            db.execute('UPDATE import_jobs SET status=?,result=?,updated_at=? WHERE id=?',
                       (status, json.dumps(result), stamp(), job_id))
            persist_import_files(db, job_id, result, stamp())
            lease = (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
            db.execute('UPDATE scheduler_settings SET lease_until=? WHERE id=1 AND active_job=?', (lease, job_id))

    def discover(self, db, html):
        for anchor in BeautifulSoup(html, 'html.parser').select('a[href]'):
            url = urljoin(BASE, anchor['href'])
            parts = urlsplit(url)
            if parts.hostname in {'ibbi.gov.in','www.ibbi.gov.in'} and parts.path.startswith('/orders/'):
                category = parts.path.removeprefix('/orders/').strip('/')
                if category and '/' not in category:
                    db.execute('INSERT OR IGNORE INTO ibbi_sources(category,url,created_at) VALUES(?,?,?)',
                               (category, f'{BASE}/orders/{category}', stamp()))

    def run(self, job_id):
        with self.connect() as db:
            selected=json.loads(db.execute('SELECT result FROM import_jobs WHERE id=?',(job_id,)).fetchone()[0]).get('category','all')
        result = {'pages':0, 'discovered':0, 'imported':0, 'duplicates':0, 'failed':0,
                  'items':[], 'errors':[], 'limits_reached':False, 'known_orders':0,'category':selected}
        heartbeat_stop = Event()
        def heartbeat():
            while not heartbeat_stop.wait(30):
                try:
                    with self.connect() as db:
                        until = (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
                        db.execute('UPDATE scheduler_settings SET lease_until=? WHERE id=1 AND active_job=?',(until,job_id))
                except Exception:
                    logging.exception('Scheduler heartbeat failed')
        heartbeat_thread = Thread(target=heartbeat,name='ibbi-heartbeat',daemon=True)
        heartbeat_thread.start()
        try:
            self.progress(job_id, result)
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=False,
                              headers={'User-Agent':'IBC-Research-Collector/1.0'}) as client:
                # Refresh menu categories before scanning, retaining known categories if discovery fails.
                try:
                    content, _ = fetch(client, f'{BASE}/orders/nclt', 15*1024*1024)
                    with self.connect() as db:
                        self.discover(db, content.decode('utf-8', errors='replace'))
                except (ValueError,httpx.HTTPError) as exc:
                    result['errors'].append({'url':f'{BASE}/orders/nclt','error':str(exc)[:500]})
                with self.connect() as db:
                    sources = [dict(r) for r in db.execute('SELECT * FROM ibbi_sources ORDER BY category' if selected=='all'
                        else 'SELECT * FROM ibbi_sources WHERE category=? ORDER BY category',() if selected=='all' else (selected,))]
                for source in sources:
                    if self.stop_event.is_set():
                        break
                    self.collect_source(client, source, job_id, result)
            status = 'interrupted' if self.stop_event.is_set() else 'completed_with_errors' if result['errors'] else 'completed'
            self.progress(job_id, result, status)
        except Exception:
            logging.exception('Automatic IBBI sync failed')
            result['error'] = 'Collector stopped unexpectedly; saved PDFs and checkpoints are retained.'
            self.progress(job_id, result, 'failed')
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=10)
            with self.connect() as db:
                interval = db.execute('SELECT interval_minutes FROM scheduler_settings WHERE id=1').fetchone()[0]
                next_run = (datetime.now(timezone.utc)+timedelta(minutes=interval)).isoformat()
                db.execute('UPDATE scheduler_settings SET lease_until=NULL,active_job=NULL,next_run=? WHERE id=1 AND active_job=?',
                           (next_run,job_id))

    def collect_source(self, client, source, job_id, result):
        started, had_errors = stamp(), False
        page = source['next_page'] if not source['last_sync'] else 1
        last = page
        with self.connect() as db:
            db.execute("UPDATE ibbi_sources SET status='running',last_attempt=?,error=NULL WHERE category=?", (started,source['category']))
        try:
            while page <= last and not self.stop_event.is_set():
                url = source['url']+'?'+urlencode({'page':page})
                content, _ = fetch(client, url, 15*1024*1024)
                rows, last, benches = parse_listing(content.decode('utf-8',errors='replace'), allow_empty=True)
                with self.connect() as db:
                    for bench_id,label in benches.items():
                        db.execute('''INSERT INTO source_filters VALUES(?,?,?,?) ON CONFLICT(key)
                            DO UPDATE SET label=excluded.label,last_seen_at=excluded.last_seen_at''',(bench_id,label,started,stamp()))
                result['pages'] += 1
                for order in rows:
                    if self.stop_event.is_set():
                        break
                    with self.connect() as db:
                        existing = db.execute('SELECT case_id,document_id,pdf_status,subject,order_date FROM source_orders WHERE pdf_url=?', (order['pdf_url'],)).fetchone()
                        db.execute('''INSERT INTO source_orders(id,pdf_url,order_date,subject,remarks,first_seen_at,last_seen_at)
                            VALUES(?,?,?,?,?,?,?) ON CONFLICT(pdf_url) DO UPDATE SET order_date=excluded.order_date,
                            subject=excluded.subject,remarks=excluded.remarks,last_seen_at=excluded.last_seen_at''',
                            (order['id'],order['pdf_url'],order['order_date'],order['subject'],order['remarks'],started,stamp()))
                        db.execute('INSERT OR IGNORE INTO order_categories VALUES(?,?)', (order['id'],source['category']))
                    # Incremental ingestion: known successful orders are skipped; new/backdated and failed orders are always checked.
                    if (existing and existing['document_id'] and existing['case_id'] and existing['pdf_status']=='downloaded'
                        and existing['subject']==order['subject'] and existing['order_date']==order['order_date']):
                        with self.connect() as db:
                            set_document_category(db,existing['document_id'],source['category'])
                        result['known_orders'] += 1
                        continue
                    result['discovered'] += 1
                    result.setdefault('since_last_sync', {})[source['category']] = source['last_sync']
                    try:
                        pdf, _ = fetch(client, order['pdf_url'], 50*1024*1024)
                        prefix = pdf.find(b'%PDF-',0,8192)
                        if prefix > 0:
                            pdf = pdf[prefix:]
                        item = self.ingest(pdf, urlsplit(order['pdf_url']).path.rsplit('/',1)[-1], order['pdf_url'])
                        result['duplicates' if item['duplicate'] else 'imported'] += 1
                        result['items'].append({'url':order['pdf_url'],'category':source['category'],**item})
                        # Events persist separately; keep only a small preview in job JSON.
                        result['items'] = result['items'][-20:]
                        with self.connect() as db:
                            set_document_category(db,item['document_id'],source['category'])
                            stored = db.execute('SELECT sha256 FROM documents WHERE id=?', (item['document_id'],)).fetchone()
                            digest = hashlib.sha256(pdf).hexdigest()
                            if not stored or stored['sha256'] != digest:
                                raise ValueError('Imported PDF hash does not match this source row')
                            db.execute("UPDATE source_orders SET case_id=?,document_id=?,pdf_sha256=?,pdf_bytes=?,pdf_status='downloaded',pdf_error=NULL,pdf_fetched_at=? WHERE id=?",
                                       (item['case_id'],item['document_id'],digest,len(pdf),stamp(),order['id']))
                    except Exception as exc:
                        from fastapi import HTTPException
                        if not isinstance(exc,(HTTPException,ValueError,httpx.HTTPError)):
                            raise
                        had_errors = True
                        error = str(exc.detail if isinstance(exc,HTTPException) else exc)[:500]
                        result['failed'] += 1
                        result['errors'].append({'url':order['pdf_url'],'error':error})
                        with self.connect() as db:
                            db.execute("UPDATE source_orders SET pdf_status='failed',pdf_error=? WHERE id=?",(error,order['id']))
                    self.progress(job_id,result)
                    self.stop_event.wait(.3)
                if self.stop_event.is_set():
                    break
                with self.connect() as db:
                    db.execute('''INSERT INTO source_pages VALUES(?,?,?,?,?) ON CONFLICT(filter_key,page_number)
                        DO UPDATE SET fetched_at=excluded.fetched_at,row_count=excluded.row_count,last_page=excluded.last_page''',
                        ('category:'+source['category'],page,stamp(),len(rows),last))
                    db.execute('DELETE FROM source_page_failures WHERE filter_key=? AND page_number=?',('category:'+source['category'],page))
                    db.execute('UPDATE ibbi_sources SET next_page=? WHERE category=?',(page+1,source['category']))
                page += 1
                self.progress(job_id,result)
                self.stop_event.wait(.5)
            with self.connect() as db:
                pending = db.execute('''SELECT COUNT(*) FROM source_orders o JOIN order_categories c ON c.order_id=o.id
                    WHERE c.category=? AND (o.pdf_status<>'downloaded' OR o.case_id IS NULL)''',(source['category'],)).fetchone()[0]
                if pending and not self.stop_event.is_set():
                    had_errors = True
                    result['errors'].append({'url':source['url'],'error':f'{pending} order PDFs remain unstored; the next sync will retry them.'})
                if not self.stop_event.is_set() and not had_errors:
                    db.execute("UPDATE ibbi_sources SET last_sync=?,next_page=1,status='completed',error=NULL WHERE category=?",(started,source['category']))
                else:
                    db.execute("UPDATE ibbi_sources SET status=?,next_page=1 WHERE category=?",
                               ('interrupted' if self.stop_event.is_set() else 'completed_with_errors',source['category']))
        except (ValueError,httpx.HTTPError) as exc:
            result['errors'].append({'url':source['url'],'error':str(exc)[:500]})
            with self.connect() as db:
                db.execute("UPDATE ibbi_sources SET status='failed',error=? WHERE category=?",(str(exc)[:500],source['category']))
                db.execute('''INSERT INTO source_page_failures(filter_key,page_number,last_error,last_attempt_at)
                    VALUES(?,?,?,?) ON CONFLICT(filter_key,page_number) DO UPDATE SET last_error=excluded.last_error,
                    last_attempt_at=excluded.last_attempt_at,attempts=source_page_failures.attempts+1''',
                    ('category:'+source['category'],page,str(exc)[:500],stamp()))
            self.progress(job_id,result)
