import hashlib
import json
import logging
from pathlib import Path
from uuid import uuid4
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from starlette.concurrency import run_in_threadpool
from .models import CaseRecord
from .automatic_orders import DEFAULT_CATEGORIES
from .document_matching import fingerprint
from .pdf_source import read_source, render_source_page, VERSION as SOURCE_VERSION
from .db import Database
from . import codex_extraction
from .config import ROOT, MAX_UPLOAD

class DocumentService:
    """Coordinate PDF storage, extraction and source viewing without SQL."""

    def __init__(self, context):
        self.context = context

    def ingest(
        self,
        content,
        filename,
        source_url,
        case_id=None,
        max_bytes=MAX_UPLOAD,
        preextracted=None,
    ):
        if len(content) > max_bytes:
            raise HTTPException(413, f'PDF exceeds the {max_bytes // (1024 * 1024)} MB limit')
        logging.getLogger('ibc.import').info('PDF ingest file=%s bytes=%s', filename, len(content))
        digest = hashlib.sha256(content).hexdigest()
        with self.context.database.session() as db:
            existing = db.document_by_hash(digest)
            if existing:
                if case_id and case_id != existing['case_id']:
                    raise HTTPException(409, 'This document already belongs to another case')
                logging.getLogger('ibc.import').info('PDF duplicate file=%s extraction_skipped=true', filename)
                return {'case_id': existing['case_id'], 'document_id': existing['id'], 'duplicate': True}
        try:
            extraction = preextracted if preextracted is not None else codex_extraction.extract_pdf(content)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        doc_id = uuid4().hex
        new_case = case_id is None
        case_id = case_id or uuid4().hex
        record = codex_extraction.record_for_document(extraction, doc_id)
        path = self.context.settings.storage / 'documents' / f'{doc_id}.pdf'
        try:
            with self.context.database.session() as db:
                db.begin()
                if new_case:
                    db.insert_case(case_id, json.dumps(record), Database.timestamp())
                else:
                    existing_case = self.context.cases.get_case(db, case_id)
                    merged = existing_case['record']
                    if existing_case['review_status'] == 'draft':
                        record = codex_extraction.fill_missing(merged, record)
                        db.replace_case_record(json.dumps(record), case_id)
                    db.mark_case_draft(case_id)
                path.write_bytes(content)
                db.insert_document(
                    doc_id,
                    case_id,
                    filename,
                    digest,
                    source_url,
                    json.dumps(extraction),
                    Database.timestamp(),
                    content,
                    extraction.get('subject'),
                )
                self.context.cases.validate_evidence(
                    db,
                    case_id,
                    CaseRecord.model_validate(
                        record if new_case or existing_case['review_status'] == 'draft' else existing_case['record'],
                    ),
                )
                db.project(
                    case_id,
                    record if new_case or existing_case['review_status'] == 'draft' else existing_case['record'],
                )
                fingerprint(db, doc_id, filename, extraction, Database.timestamp())
                self.context.cases.audit(
                    db,
                    case_id,
                    'document_added',
                    {'document_id': doc_id, 'sha256': digest, 'provider': 'codex_sdk'},
                )
        except Database.INTEGRITY_ERRORS:
            path.unlink(missing_ok=True)
            with self.context.database.session() as db:
                existing = db.document_by_hash(digest)
            if existing:
                if not new_case and case_id != existing['case_id']:
                    raise HTTPException(409, 'This document already belongs to another case')
                return {'case_id': existing['case_id'], 'document_id': existing['id'], 'duplicate': True}
            raise
        except Exception:
            path.unlink(missing_ok=True)
            raise
        logging.getLogger('ibc.import').info('PDF saved file=%s pages=%s document_id=%s', filename, len(extraction.get('pages', [])), doc_id)
        return {'case_id': case_id, 'document_id': doc_id, 'duplicate': False}

    def document_content(self, doc_id: str):
        with self.context.database.session() as db:
            row = db.document_source(doc_id)
            cached = db.cached_source_evidence(doc_id)
        if not row:
            raise HTTPException(404, 'Document not found')
        if cached and cached['sha256'] == row['sha256'] and (cached['reader_version'] == SOURCE_VERSION):
            source_content = json.loads(cached['content'])
        else:
            try:
                content = bytes(row['content']) if row['content'] is not None else (self.context.settings.storage / 'documents' / f'{doc_id}.pdf').read_bytes()
                source_content = read_source(content)
            except (ValueError, OSError) as exc:
                raise HTTPException(422, str(exc)) from exc
            with self.context.database.session() as db:
                db.cache_source_evidence(
                    doc_id,
                    row['sha256'],
                    SOURCE_VERSION,
                    json.dumps(source_content),
                )
        extraction = json.loads(row['extraction'])
        result = {
            'filename': row['filename'],
            **extraction,
            'pages': source_content['pages'],
            'warnings': source_content['warnings'],
            'source_provider': 'python',
            'source_reader_version': SOURCE_VERSION,
        }
        with self.context.database.session() as db:
            source = db.document_official_subject(doc_id)
        result['pdf_subject'] = result.get('subject')
        if source:
            result['subject'] = source['subject']
        return result

    def source_page_image(self, doc_id: str, page_number: int):
        with self.context.database.session() as db:
            row = db.stored_pdf_bytes(doc_id)
        if not row:
            raise HTTPException(404, 'Document not found')
        try:
            content = bytes(row['content']) if row['content'] is not None else (self.context.settings.storage / 'documents' / f'{doc_id}.pdf').read_bytes()
            png = render_source_page(content, page_number)
        except IndexError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return Response(png, media_type='image/png')

    def populate_facts(self, doc_id: str):
        with self.context.database.session() as db:
            row = db.document_for_extraction(doc_id)
            if not row:
                raise HTTPException(404, 'Document not found')
            case = self.context.cases.get_case(db, row['case_id'])
            if case['review_status'] != 'draft':
                raise HTTPException(409, 'Only draft cases can be extracted again')
            content = bytes(row['content']) if row['content'] is not None else (self.context.settings.storage / 'documents' / f'{doc_id}.pdf').read_bytes()
            try:
                extraction = codex_extraction.extract_pdf(content)
                facts = codex_extraction.record_for_document(extraction, doc_id)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            report = extraction['case_facts']
            history = db.case_audit_snapshots(case['id'])
            automatic = sum((item['action'] == 'automatic_facts_extracted' for item in history))
            manual = any(
                (item['action'] not in {'document_added', 'automatic_facts_extracted'} for item in history),
            )
            starter = any(
                (json.loads(item['snapshot']).get('starter_record') for item in history if item['action'] == 'document_added'),
            )
            document_count = db.case_document_count(case['id'])[0]
            auto_created = case['review_status'] == 'draft' and case['version'] == 1 + automatic and (document_count == 1) and (not manual) and (not starter)
            merged = facts if auto_created else codex_extraction.fill_missing(case['record'], facts)
            extraction['case_facts'] = report
            extraction['warnings'].extend(
                (w for w in report['warnings'] if w not in extraction['warnings']),
            )
            db.update_document_extraction(json.dumps(extraction), doc_id)
            db.update_document_subject(extraction.get('subject'), doc_id)
            self.context.cases.validate_evidence(db, case['id'], CaseRecord.model_validate(merged))
            db.delete_document_matches(doc_id, doc_id)
            db.delete_document_identifiers(doc_id)
            db.delete_document_fingerprints(doc_id)
            fingerprint(db, doc_id, row['filename'], extraction, row['created_at'])
            if case['review_status'] == 'draft' and merged != CaseRecord.model_validate(case['record']).model_dump(mode='json'):
                updated = db.refresh_case_draft(json.dumps(merged), case['id'], case['version'], 'draft')
                if updated != 1:
                    raise HTTPException(409, 'Case changed; reload and retry extraction')
                db.project(case['id'], merged)
                self.context.cases.audit(
                    db,
                    case['id'],
                    'automatic_facts_extracted',
                    {'document_id': doc_id, 'extraction_report': report},
                )
            return {
                'status': 'extracted',
                'case_id': case['id'],
                'evidence_count': len(report['evidence']),
            }

    def sample_info(self):
        return json.loads((ROOT / 'samples/manifest.json').read_text(encoding='utf-8'))

    def import_sample(self):
        manifest = self.context.documents.sample_info()
        path = ROOT / 'samples' / manifest['filename']
        if not path.exists():
            raise HTTPException(404, 'Bundled sample PDF is missing')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest['sha256']:
            raise HTTPException(409, 'Sample checksum differs from the reviewed fixture')
        return self.context.documents.ingest(content, path.name, manifest['source_url'])

    def sample_pdf(self):
        path = ROOT / 'samples' / self.context.documents.sample_info()['filename']
        if not path.exists():
            raise HTTPException(404, 'Sample PDF missing')
        return FileResponse(path, media_type='application/pdf')

    async def upload(self, request: Request):
        job_id = uuid4().hex
        filename = 'document.pdf'
        job_label = 'File upload'

        def save_job(status, result):
            stamp = Database.timestamp()
            with self.context.database.session() as db:
                existing = db.import_job_exists(job_id)
                if existing:
                    db.update_upload_job(job_label, status, json.dumps(result), stamp, job_id)
                else:
                    db.insert_import_job(
                        job_id,
                        job_label,
                        status,
                        stamp,
                        stamp,
                        json.dumps(result),
                    )
                db.persist_import_files(job_id, result, stamp)

        def failed_upload(exc):
            if isinstance(exc, HTTPException):
                status_code, detail = (exc.status_code, str(exc.detail))
            else:
                logging.exception('File upload job %s failed', job_id)
                status_code, detail = (500, 'Upload failed unexpectedly. Check the server logs and retry.')
            result = {
                'kind': 'file_upload',
                'filename': filename,
                'pages': 0,
                'discovered': 1,
                'imported': 0,
                'duplicates': 0,
                'skipped': 0,
                'review_required': 0,
                'failed': 1,
                'errors': [{'url': filename, 'error': detail}],
                'error': detail,
                'items': [],
            }
            save_job('failed', result)
            return JSONResponse(
                status_code=status_code,
                content={'detail': detail, 'import_job_id': job_id},
            )
        try:
            form = await request.form()
            file = form.get('file')
            if file is None or not hasattr(file, 'file'):
                raise HTTPException(422, 'Select a PDF document to upload')
            filename = Path((getattr(file, 'filename', '') or filename).replace('\\', '/')).name
            job_label = f'File upload: {filename}'
            source_url = str(form.get('source_url') or '')
            case_id = str(form.get('case_id') or '')
            order_kind = str(form.get('order_kind') or 'unknown')
            save_job(
                'running',
                {
                    'kind': 'file_upload',
                    'filename': filename,
                    'pages': 0,
                    'discovered': 1,
                    'imported': 0,
                    'duplicates': 0,
                    'skipped': 0,
                    'review_required': 0,
                    'failed': 0,
                    'errors': [],
                    'items': [],
                },
            )
            if order_kind not in {*DEFAULT_CATEGORIES, 'unknown'}:
                raise HTTPException(422, 'Unknown court/authority category')
            if source_url and (len(source_url) > 2000 or not source_url.startswith(('https://', 'http://'))):
                raise HTTPException(422, 'Source URL must use http or https')
            content = file.file.read(MAX_UPLOAD + 1)
            result = await run_in_threadpool(
                self.context.documents.ingest,
                content,
                filename,
                source_url or None,
                case_id or None,
            )
            if order_kind != 'unknown' and (not result['duplicate']):
                with self.context.database.session() as db:
                    db.set_document_category(result['document_id'], order_kind)
            if result['duplicate']:
                with self.context.database.session() as db:
                    original = db.document_metadata(result['document_id'])
                result['original'] = dict(original)
            item_url = source_url or f'upload:///{filename}'
            job_result = {
                'kind': 'file_upload',
                'filename': filename,
                'pages': 0,
                'discovered': 1,
                'imported': int(not result['duplicate']),
                'duplicates': int(result['duplicate']),
                'skipped': 0,
                'review_required': 0,
                'failed': 0,
                'errors': [],
                'items': [{
                    'url': item_url,
                    'document_id': result['document_id'],
                    'case_id': result['case_id'],
                    'duplicate': result['duplicate'],
                }],
            }
            save_job('completed', job_result)
            return {**result, 'import_job_id': job_id}
        except Exception as exc:
            return failed_upload(exc)

    def document_pdf(self, doc_id: str, download: bool=False):
        from urllib.parse import quote
        with self.context.database.session() as db:
            row = db.stored_pdf(doc_id)
            if not row:
                raise HTTPException(404, 'Document not found')
            if row['content'] is not None:
                headers = {
                    'Content-Disposition': "attachment; filename*=UTF-8''" + quote(row['filename'] or doc_id + '.pdf', safe=''),
                } if download else None
                return Response(bytes(row['content']), media_type='application/pdf', headers=headers)
        return FileResponse(
            self.context.settings.storage / 'documents' / f'{row['id']}.pdf',
            media_type='application/pdf',
            filename=row['filename'] or doc_id + '.pdf' if download else None,
        )
