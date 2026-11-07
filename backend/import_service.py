import json
import logging
from uuid import uuid4
from urllib.parse import unquote, urlsplit
from fastapi import HTTPException
from .ibbi_orders import download_order
from .url_import import ImportRequest, public_url, crawl
from .db import Database
from . import codex_extraction

class ImportService:
    """Coordinate background imports, history and source-order review."""

    def __init__(self, context):
        self.context = context

    def run_import(self, job_id, payload):

        def progress(result, status='running'):
            with self.context.database.session() as db:
                db.update_import_progress(status, json.dumps(result), Database.timestamp(), job_id)
                db.persist_import_files(job_id, result, Database.timestamp())
        try:
            progress({})
            result = crawl(payload, self.context.documents.ingest, progress)
            progress(result, 'completed_with_errors' if result['errors'] else 'completed')
            logging.getLogger('ibc.import').info('Import finished job=%s found=%s imported=%s duplicates=%s review=%s failed=%s', job_id, result['discovered'], result['imported'], result['duplicates'], result['review_required'], result['failed'])
        except Exception:
            logging.exception('URL import job %s failed', job_id)
            with self.context.database.session() as db:
                row = db.import_job_result(job_id)
            result = json.loads(row['result'])
            result['error'] = 'Import stopped unexpectedly. Imported documents are retained; check server logs and retry.'
            progress(result, 'failed')
        finally:
            self.context.import_slots.release()

    def import_url(self, payload: ImportRequest):
        try:
            payload.url = public_url(payload.url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not self.context.import_slots.acquire(blocking=False):
            raise HTTPException(429, 'Import queue is full; wait for a current import to finish')
        job_id = uuid4().hex
        try:
            with self.context.database.session() as db:
                db.insert_import_job(
                    job_id,
                    payload.url,
                    'queued',
                    Database.timestamp(),
                    Database.timestamp(),
                    '{}',
                )
            self.context.executor.submit(self.context.imports.run_import, job_id, payload)
        except Exception:
            self.context.import_slots.release()
            raise
        return {'id': job_id, 'status': 'queued'}

    def imports(self):
        with self.context.database.session() as db:
            return [{**dict(r), 'result': json.loads(r['result'])} for r in db.recent_import_jobs()]

    def import_status(self, job_id: str):
        with self.context.database.session() as db:
            row = db.import_job(job_id)
        if not row:
            raise HTTPException(404, 'Import job not found')
        return {**dict(row), 'result': json.loads(row['result'])}

    def import_history(self, page: int=1, page_size: int=25):
        with self.context.database.session() as db:
            total = db.import_job_count()[0]
            totals = {key: 0 for key in ('discovered', 'imported', 'duplicates', 'skipped', 'review_required', 'failed')}
            for row in db.execute('SELECT result FROM import_jobs').fetchall():
                counts = json.loads(row['result'])
                for key in totals:
                    totals[key] += counts.get(key, 0)
            jobs = db.import_job_page(page_size, (page - 1) * page_size)
            job_subjects = {job['id']: [r['subject'] for r in db.import_job_subjects(job['id'])] for job in jobs}
        items = []
        for job in jobs:
            result = json.loads(job['result'])
            items.append(
                {
                    'id': job['id'],
                    'url': job['url'],
                    'status': job['status'],
                    'subjects': job_subjects[job['id']],
                    'created_at': job['created_at'],
                    'updated_at': job['updated_at'],
                    **{key: result.get(key, 0) for key in (
                        'pages',
                        'discovered',
                        'imported',
                        'duplicates',
                        'skipped',
                        'review_required',
                        'failed',
                    )},
                    'phase': result.get('phase'),
                    'processed': result.get('processed', 0),
                    'current_file': result.get('current_file'),
                    'errors': len(result.get('errors', [])),
                },
            )
        return {'items': items, 'total': total, 'totals': totals, 'page': page, 'page_size': page_size}

    def import_files(
        self,
        page: int=1,
        page_size: int=25,
        q: str='',
        duplicate: str='all',
        job_id: str='',
    ):
        if duplicate not in {'all', 'yes', 'no'}:
            raise HTTPException(422, 'Invalid duplicate filter')
        with self.context.database.session() as db:
            count, rows = db.list_import_files(page, page_size, q, duplicate, job_id)
        for row in rows:
            source_name = unquote(urlsplit(row['source_url'] or '').path.rsplit('/', 1)[-1])
            row['imported_filename'] = source_name or row['filename']
            row['duplicate_of'] = {
                'document_id': row['document_id'],
                'filename': row['filename'],
                'source_url': row['stored_source_url'],
                'created_at': row['document_created_at'],
            } if row['duplicate'] else None
        return {'items': rows, 'total': count, 'page': page, 'page_size': page_size}

    def unified_catalogue(
        self,
        page: int=1,
        page_size: int=25,
        q: str='',
        category: str='',
        bench: str='',
        remark: str='',
        adverse: str='',
        date_from: str='',
        date_to: str='',
        duplicate: str='all',
    ):
        if adverse not in {'', 'ip', 'other'} or duplicate not in {'all', 'yes', 'no'}:
            raise HTTPException(422, 'Invalid catalogue filter')
        for value in (date_from, date_to):
            if value and (len(value) != 10 or not value[:4].isdigit()):
                raise HTTPException(422, 'Invalid date')
        with self.context.database.session() as db:
            return db.catalogue_entries(
                page=page,
                page_size=page_size,
                q=q,
                category=category,
                bench=bench,
                remark=remark,
                adverse=adverse,
                date_from=date_from,
                date_to=date_to,
                duplicate=duplicate,
            )

    def order_stats(self):
        with self.context.database.session() as db:
            counts = db.source_order_counts()
            pages = db.source_page_counts('category:%')
            failed_pages = db.source_page_failure_count('category:%')[0]
        return {
            'orders': counts['total'],
            'pdfs_downloaded': counts['downloaded'],
            'pdfs_failed': counts['failed'],
            'in_review': counts['in_review'],
            'pages_fetched': pages['done'],
            'pages_expected': pages['expected'],
            'pages_failed': failed_pages,
        }

    def order_filters(self):
        with self.context.database.session() as db:
            benches = [dict(r) for r in db.source_filters()]
            remarks = [r['remarks'] for r in db.source_remarks()]
            categories = [r['category'] for r in db.source_categories()]
        return {'benches': benches, 'remarks': remarks, 'categories': categories}

    def order_summary(self):
        with self.context.database.session() as db:
            by_year = [dict(r) for r in db.source_orders_by_year()]
            by_remark = [dict(r) for r in db.source_orders_by_remark()]
            date_range = db.source_order_date_range()
        return {
            'by_year': by_year,
            'top_remarks': by_remark,
            'earliest': date_range['earliest'],
            'latest': date_range['latest'],
        }

    def list_orders(
        self,
        q: str='',
        bench: str='',
        remark: str='',
        adverse: str='',
        category: str='',
        date_from: str='',
        date_to: str='',
        page: int=1,
        page_size: int=25,
    ):
        if adverse not in {'', 'ip', 'other'}:
            raise HTTPException(422, 'Invalid adverse filter')
        if date_from and (len(date_from) != 10 or not date_from[:4].isdigit()):
            raise HTTPException(422, 'Invalid start date')
        if date_to and (len(date_to) != 10 or not date_to[:4].isdigit()):
            raise HTTPException(422, 'Invalid end date')
        with self.context.database.session() as db:
            total, rows = db.list_source_orders(
                q,
                bench,
                remark,
                adverse,
                category,
                date_from,
                date_to,
                page,
                page_size,
            )
        return {'total': total, 'page': page, 'page_size': page_size, 'items': rows}

    def review_order(self, order_id: str):
        with self.context.database.session() as db:
            row = db.source_order(order_id)
            if not row:
                raise HTTPException(404, 'Source order not found')
            if row['case_id']:
                return {'case_id': row['case_id'], 'duplicate': True}
        try:
            path = download_order(self.context.settings.storage, order_id)
            content = path.read_bytes()
            extraction = codex_extraction.extract_pdf(content)
            decision = codex_extraction.final_decision(extraction)
            if decision is None:
                raise HTTPException(
                    422,
                    'This PDF does not establish final plan approval or a liquidation order',
                )
            result = self.context.documents.ingest(
                content,
                path.name,
                row['pdf_url'],
                preextracted=extraction,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(502, f'IBBI PDF could not be downloaded: {exc}') from exc
        with self.context.database.session() as db:
            db.link_source_document(result['case_id'], result['document_id'], order_id)
        return result
