import hashlib
import json
import os
import logging
import sqlite3
import psycopg
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import connection, decode, initialize
from .extraction import extract_pdf
from .ibbi_orders import download_order
from .models import Approval, CaseRecord, SaveRecord, metrics
from .structured import project, match_candidates, persist_import_files
from .url_import import ImportRequest, public_url, crawl
from .research_analytics import case_profiles, research_analytics, select_recent_cases
from .automatic_orders import AutomaticOrders, ScheduleSettings, DEFAULT_CATEGORIES, set_document_category
from .document_matching import fingerprint, candidates as document_candidates
from .case_extraction import extract_case, fill_missing
from .catalogue import catalogue_entries

ROOT = Path(__file__).resolve().parent.parent
MAX_UPLOAD = 15 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def create_app(data_dir: Path | None = None):
    storage = data_dir or Path(os.environ.get("IBC_DATA_DIR", ROOT / "data"))
    database_url = os.environ.get("DATABASE_URL", "") if data_dir is None else ""

    def db_connection():
        return connection(storage, database_url=database_url)

    @asynccontextmanager
    async def lifespan(app):
        initialize(storage, database_url=database_url)
        with db_connection() as db:
            db.execute("UPDATE import_jobs SET status='interrupted' WHERE status IN ('queued','running') AND id NOT IN (SELECT active_job FROM scheduler_settings WHERE active_job IS NOT NULL AND lease_until>?)", (now(),))
        collector.start()
        yield
        collector.close()
        executor.shutdown(wait=True)

    app = FastAPI(title="IBC Evidence Explorer", version="0.1.0", lifespan=lifespan)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-import")
    import_slots = BoundedSemaphore(3)

    async def database_error(request, exc):
        logging.error("Database operation failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Database operation failed. Check PostgreSQL availability and server logs, then retry."})

    app.add_exception_handler(sqlite3.DatabaseError, database_error)
    app.add_exception_handler(psycopg.Error, database_error)

    def get_case(db, case_id):
        row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Case not found")
        return decode(row)

    def audit(db, case_id, action, record, actor=None):
        db.execute("INSERT INTO audit(case_id,action,snapshot,actor,created_at) VALUES(?,?,?,?,?)",
                   (case_id, action, json.dumps(record), actor, now()))

    def validate_evidence(db, case_id, record):
        docs = {r["id"]: len(json.loads(r["extraction"])["pages"]) for r in
                db.execute("SELECT id,extraction FROM documents WHERE case_id=?", (case_id,))}

        def visit(value):
            if isinstance(value, dict):
                if "document_id" in value:
                    if value["document_id"] not in docs or not 1 <= value["page"] <= docs[value["document_id"]]:
                        raise HTTPException(422, "Evidence must reference a valid page in a document attached to this case")
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(record.model_dump(mode="json"))

    def ingest(content, filename, source_url, case_id=None, sample_record=None, max_bytes=MAX_UPLOAD):
        if len(content) > max_bytes:
            raise HTTPException(413, f"PDF exceeds the {max_bytes//(1024*1024)} MB limit")
        digest = hashlib.sha256(content).hexdigest()
        with db_connection() as db:
            existing = db.execute("SELECT id,case_id FROM documents WHERE sha256=?", (digest,)).fetchone()
            if existing:
                if case_id and case_id != existing["case_id"]:
                    raise HTTPException(409, "This document already belongs to another case")
                return {"case_id": existing["case_id"], "document_id": existing["id"], "duplicate": True}
        try:
            extraction = extract_pdf(content)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        doc_id = uuid4().hex
        new_case = case_id is None
        case_id = case_id or uuid4().hex
        record = CaseRecord().model_dump(mode="json")
        if not sample_record:
            record, extraction['case_facts'] = extract_case(extraction,doc_id)
            extraction['warnings'].extend(extraction['case_facts']['warnings'])
        if sample_record:
            # Curated starter data is explicitly separate from generic extraction.
            serialized = json.dumps(sample_record).replace("SAMPLE_DOCUMENT", doc_id)
            record = CaseRecord.model_validate_json(serialized).model_dump(mode="json")
        path = storage / "documents" / f"{doc_id}.pdf"
        try:
            with db_connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if new_case:
                    db.execute("INSERT INTO cases(id,record,created_at) VALUES(?,?,?)", (case_id, json.dumps(record), now()))
                else:
                    existing_case = get_case(db, case_id)
                    # Fill missing draft fields without replacing reviewed or previously entered facts.
                    merged = existing_case['record']
                    if existing_case['review_status']=='draft':
                        record=fill_missing(merged,record)
                        db.execute('UPDATE cases SET record=? WHERE id=?',(json.dumps(record),case_id))
                    db.execute("UPDATE cases SET review_status='draft',version=version+1,reviewed_by=NULL,reviewed_at=NULL WHERE id=?", (case_id,))
                path.write_bytes(content)
                db.execute("""INSERT INTO documents
                    (id,case_id,filename,sha256,source_url,extraction,created_at,content,subject)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                           (doc_id, case_id, filename, digest, source_url, json.dumps(extraction), now(), content, extraction.get("subject")))
                validate_evidence(db, case_id, CaseRecord.model_validate(record if new_case or existing_case['review_status']=='draft' else existing_case['record']))
                project(db, case_id, record if new_case or existing_case['review_status']=='draft' else existing_case['record'])
                fingerprint(db, doc_id, filename, extraction, now())
                audit(db, case_id, "document_added", {"document_id": doc_id, "sha256": digest, "starter_record": record if sample_record else None})
        except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation):
            path.unlink(missing_ok=True)
            with db_connection() as db:
                existing = db.execute("SELECT id,case_id FROM documents WHERE sha256=?", (digest,)).fetchone()
            if existing:
                if not new_case and case_id != existing["case_id"]:
                    raise HTTPException(409, "This document already belongs to another case")
                return {"case_id": existing["case_id"], "document_id": existing["id"], "duplicate": True}
            raise
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {"case_id": case_id, "document_id": doc_id, "duplicate": False}

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    collector = AutomaticOrders(db_connection, lambda content, filename, url: ingest(content,filename,url,max_bytes=50*1024*1024))

    @app.get('/api/scheduler')
    def scheduler_status():
        return collector.status()

    @app.put('/api/scheduler')
    def configure_scheduler(payload: ScheduleSettings):
        try:
            return collector.configure(payload)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc

    @app.post('/api/scheduler/sync', status_code=202)
    def scheduler_sync(category: str | None = Query(None,max_length=100)):
        try:
            job_id = collector.trigger(category)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
        if not job_id:
            raise HTTPException(409, 'An automatic sync is already running')
        return {'id':job_id,'status':'queued'}

    def run_import(job_id, payload):
        def progress(result, status="running"):
            with db_connection() as db:
                db.execute("UPDATE import_jobs SET status=?,result=?,updated_at=? WHERE id=?",
                           (status, json.dumps(result), now(), job_id))
                persist_import_files(db, job_id, result, now())
        try:
            progress({})
            result = crawl(payload, ingest, progress)
            progress(result, "completed_with_errors" if result["errors"] else "completed")
        except Exception:
            logging.exception("URL import job %s failed", job_id)
            with db_connection() as db:
                row = db.execute("SELECT result FROM import_jobs WHERE id=?", (job_id,)).fetchone()
            result = json.loads(row["result"])
            result["error"] = "Import stopped unexpectedly. Imported documents are retained; check server logs and retry."
            progress(result, "failed")
        finally:
            import_slots.release()

    @app.post("/api/imports", status_code=202)
    def import_url(payload: ImportRequest):
        try:
            payload.url = public_url(payload.url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not import_slots.acquire(blocking=False):
            raise HTTPException(429, "Import queue is full; wait for a current import to finish")
        job_id = uuid4().hex
        try:
            with db_connection() as db:
                db.execute("INSERT INTO import_jobs VALUES(?,?,?,?,?,?)",
                           (job_id, payload.url, "queued", now(), now(), "{}"))
            executor.submit(run_import, job_id, payload)
        except Exception:
            import_slots.release()
            raise
        return {"id": job_id, "status": "queued"}

    @app.get("/api/imports")
    def imports():
        with db_connection() as db:
            return [{**dict(r), "result": json.loads(r["result"])} for r in
                    db.execute("SELECT * FROM import_jobs ORDER BY created_at DESC LIMIT 20")]

    @app.get("/api/imports/{job_id}")
    def import_status(job_id: str):
        with db_connection() as db:
            row = db.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Import job not found")
        return {**dict(row), "result": json.loads(row["result"])}

    @app.get("/api/import-history")
    def import_history(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
        with db_connection() as db:
            total = db.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0]
            jobs = db.execute("SELECT * FROM import_jobs ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                              (page_size, (page-1)*page_size)).fetchall()
            job_subjects = {job["id"]: [r["subject"] for r in db.execute(
                "SELECT DISTINCT COALESCE(o.subject,d.subject) subject FROM import_files f JOIN documents d ON d.id=f.document_id LEFT JOIN source_orders o ON o.pdf_url=f.source_url WHERE f.job_id=? AND COALESCE(o.subject,d.subject) IS NOT NULL ORDER BY subject LIMIT 3",
                (job["id"],))] for job in jobs}
        items = []
        for job in jobs:
            result = json.loads(job["result"])
            items.append({"id": job["id"], "url": job["url"], "status": job["status"],
                          "subjects": job_subjects[job["id"]],
                          "created_at": job["created_at"], "updated_at": job["updated_at"],
                          **{key: result.get(key, 0) for key in ("pages", "discovered", "imported", "duplicates", "failed")},
                          "errors": len(result.get("errors", []))})
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @app.get("/api/entities/match")
    def entities_match(name: str = Query("", max_length=500),
                       din: str | None = Query(None, pattern=r"^\d{8}$"),
                       cin: str | None = Query(None, pattern=r"^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$")):
        if not name and not din and not cin:
            raise HTTPException(422, "Provide name, DIN or CIN")
        with db_connection() as db:
            return match_candidates(db, name, din, cin)

    @app.get("/api/import-files")
    def import_files(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                     q: str = Query("", max_length=200), duplicate: str = "all",
                     job_id: str = Query("", max_length=32)):
        if duplicate not in {"all", "yes", "no"}:
            raise HTTPException(422, "Invalid duplicate filter")
        source = """SELECT f.id,f.job_id,f.document_id,d.case_id,d.filename,f.source_url,
                    f.duplicate,f.created_at,d.sha256,d.source_url AS stored_source_url,d.created_at AS document_created_at,
                    COALESCE(o.subject,d.subject) subject,d.order_kind,o.order_date,o.id source_order_id,d.subject pdf_subject
                    FROM import_files f JOIN documents d ON d.id=f.document_id LEFT JOIN source_orders o ON o.pdf_url=f.source_url
                    UNION ALL SELECT d.id,NULL,d.id,d.case_id,d.filename,d.source_url,0,d.created_at,d.sha256,d.source_url,d.created_at,
                    COALESCE(o.subject,d.subject),d.order_kind,o.order_date,o.id,d.subject
                    FROM documents d LEFT JOIN source_orders o ON o.pdf_url=d.source_url WHERE NOT EXISTS(SELECT 1 FROM import_files f WHERE f.document_id=d.id)"""
        where, params = ["(LOWER(filename) LIKE ? OR LOWER(COALESCE(source_url,'')) LIKE ? OR LOWER(COALESCE(subject,'')) LIKE ?)"], [f"%{q.lower()}%"]*3
        if duplicate != "all":
            where.append("duplicate=?")
            params.append(int(duplicate == "yes"))
        if job_id:
            where.append("job_id=?")
            params.append(job_id)
        filtered = f"FROM ({source}) AS files WHERE " + " AND ".join(where)
        with db_connection() as db:
            count = db.execute("SELECT COUNT(*) " + filtered, params).fetchone()[0]
            ordering = "CASE WHEN order_date IS NULL THEN 1 ELSE 0 END,order_date DESC,created_at ASC,id" if job_id else "created_at DESC,id"
            rows = [dict(r) for r in db.execute("SELECT * " + filtered + f" ORDER BY {ordering} LIMIT ? OFFSET ?",
                                               (*params, page_size, (page-1)*page_size))]
            for row in rows:
                row['possible_matches'] = document_candidates(db,row['document_id'])
                row['categories'] = [r['category'] for r in db.execute('SELECT category FROM document_categories WHERE document_id=? ORDER BY category',(row['document_id'],))]
        for row in rows:
            source_name = unquote(urlsplit(row["source_url"] or "").path.rsplit("/", 1)[-1])
            row["imported_filename"] = source_name or row["filename"]
            row["duplicate_of"] = {"document_id": row["document_id"], "filename": row["filename"],
                                   "source_url": row["stored_source_url"], "created_at": row["document_created_at"]} if row["duplicate"] else None
        return {"items": rows, "total": count, "page": page, "page_size": page_size}

    @app.get("/api/documents/{doc_id}/content")
    def document_content(doc_id: str):
        with db_connection() as db:
            row = db.execute("SELECT filename,extraction FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Document not found")
        extraction=json.loads(row['extraction'])
        if any(len(p['text'].strip())<30 and not p.get('ocr_attempted') for p in extraction['pages']):
            populate_facts(doc_id)
            with db_connection() as db:
                row=db.execute('SELECT filename,extraction FROM documents WHERE id=?',(doc_id,)).fetchone()
        result = {"filename": row["filename"], **json.loads(row["extraction"])}
        with db_connection() as db:
            source = db.execute('SELECT subject FROM source_orders WHERE document_id=? ORDER BY order_date DESC LIMIT 1', (doc_id,)).fetchone()
        result['pdf_subject'] = result.get('subject')
        if source:
            result['subject'] = source['subject']
        return result

    @app.post('/api/documents/{doc_id}/extract-facts')
    def populate_facts(doc_id: str):
        with db_connection() as db:
            row=db.execute('SELECT case_id,content,extraction,filename,created_at FROM documents WHERE id=?',(doc_id,)).fetchone()
            if not row:
                raise HTTPException(404,'Document not found')
            case=get_case(db,row['case_id'])
            extraction=json.loads(row['extraction'])
            if not all('layout_text' in p for p in extraction['pages']) or any(len(p['text'].strip())<30 for p in extraction['pages']):
                content=bytes(row['content']) if row['content'] is not None else (storage/'documents'/f'{doc_id}.pdf').read_bytes()
                extraction=extract_pdf(content)
            facts,report=extract_case(extraction,doc_id)
            merged=fill_missing(case['record'],facts)
            extraction['case_facts']=report
            extraction['warnings'].extend(w for w in report['warnings'] if w not in extraction['warnings'])
            validate_evidence(db,case['id'],CaseRecord.model_validate(merged))
            db.execute('UPDATE documents SET extraction=? WHERE id=?',(json.dumps(extraction),doc_id))
            db.execute('UPDATE documents SET subject=COALESCE(subject,?) WHERE id=?',(extraction.get('subject'),doc_id))
            db.execute('DELETE FROM document_matches WHERE document_id=? OR matched_document_id=?',(doc_id,doc_id))
            db.execute('DELETE FROM document_identifiers WHERE document_id=?',(doc_id,))
            db.execute('DELETE FROM document_fingerprints WHERE document_id=?',(doc_id,))
            fingerprint(db,doc_id,row['filename'],extraction,row['created_at'])
            if case['review_status']=='draft' and merged!=CaseRecord.model_validate(case['record']).model_dump(mode='json'):
                updated=db.execute('UPDATE cases SET record=?,version=version+1 WHERE id=? AND version=? AND review_status=?',
                    (json.dumps(merged),case['id'],case['version'],'draft'))
                if updated.rowcount!=1:
                    raise HTTPException(409,'Case changed; reload and retry extraction')
                project(db,case['id'],merged)
                audit(db,case['id'],'automatic_facts_extracted',{'document_id':doc_id,'extraction_report':report})
            return {'status':'extracted','case_id':case['id'],'evidence_count':len(report['evidence'])}

    @app.get("/api/sample")
    def sample_info():
        return json.loads((ROOT / "samples/manifest.json").read_text(encoding="utf-8"))

    @app.post("/api/sample/import")
    def import_sample():
        manifest = sample_info()
        path = ROOT / "samples" / manifest["filename"]
        if not path.exists():
            raise HTTPException(404, "Bundled sample PDF is missing")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest["sha256"]:
            raise HTTPException(409, "Sample checksum differs from the reviewed fixture")
        record = json.loads((ROOT / "samples/starter-record.json").read_text(encoding="utf-8"))
        return ingest(content, path.name, manifest["source_url"], sample_record=record)

    @app.get("/api/sample/pdf")
    def sample_pdf():
        path = ROOT / "samples" / sample_info()["filename"]
        if not path.exists():
            raise HTTPException(404, "Sample PDF missing")
        return FileResponse(path, media_type="application/pdf")

    @app.post("/api/documents")
    def upload(file: UploadFile = File(...), source_url: str = Form(""), case_id: str = Form(""), order_kind: str = Form('unknown')):
        if order_kind not in {*DEFAULT_CATEGORIES,'unknown'}:
            raise HTTPException(422,'Unknown court/authority category')
        if source_url and (len(source_url) > 2000 or not source_url.startswith(("https://", "http://"))):
            raise HTTPException(422, "Source URL must use http or https")
        content = file.file.read(MAX_UPLOAD + 1)
        result = ingest(content, Path((file.filename or "document.pdf").replace("\\", "/")).name,
                      source_url or None, case_id or None)
        if order_kind!='unknown' and not result['duplicate']:
            with db_connection() as db:
                set_document_category(db,result['document_id'],order_kind)
        if result['duplicate']:
            with db_connection() as db:
                original = db.execute('''SELECT filename,subject,source_url,created_at,order_kind,sha256
                    FROM documents WHERE id=?''',(result['document_id'],)).fetchone()
            result['original'] = dict(original)
        return result

    @app.get('/api/catalogue')
    def unified_catalogue(page: int = Query(1,ge=1), page_size: int = Query(25,ge=1,le=100),
                          q: str = Query('',max_length=200), category: str = Query('',max_length=100),
                          bench: str = '', remark: str = '', adverse: str = '',
                          date_from: str = '', date_to: str = '', duplicate: str = 'all'):
        if adverse not in {'','ip','other'} or duplicate not in {'all','yes','no'}:
            raise HTTPException(422,'Invalid catalogue filter')
        for value in (date_from,date_to):
            if value and (len(value)!=10 or not value[:4].isdigit()):
                raise HTTPException(422,'Invalid date')
        with db_connection() as db:
            return catalogue_entries(db,page=page,page_size=page_size,q=q,category=category,
                                     bench=bench,remark=remark,adverse=adverse,
                                     date_from=date_from,date_to=date_to,duplicate=duplicate)

    @app.get("/api/orders/stats")
    def order_stats():
        with db_connection() as db:
            counts = db.execute("""SELECT COUNT(*) total,
                COALESCE(SUM(CASE WHEN pdf_status='downloaded' THEN 1 ELSE 0 END),0) downloaded,
                COALESCE(SUM(CASE WHEN pdf_status='failed' THEN 1 ELSE 0 END),0) failed,
                COALESCE(SUM(CASE WHEN case_id IS NOT NULL THEN 1 ELSE 0 END),0) in_review
                FROM source_orders""").fetchone()
            pages = db.execute("SELECT COUNT(*) done,MAX(last_page) expected FROM source_pages WHERE filter_key='all' OR filter_key LIKE ?",('category:%',)).fetchone()
            failed_pages = db.execute("SELECT COUNT(*) FROM source_page_failures WHERE filter_key='all' OR filter_key LIKE ?",('category:%',)).fetchone()[0]
        return {"orders": counts["total"], "pdfs_downloaded": counts["downloaded"],
                "pdfs_failed": counts["failed"], "in_review": counts["in_review"],
                "pages_fetched": pages["done"], "pages_expected": pages["expected"],
                "pages_failed": failed_pages}

    @app.get("/api/orders/filters")
    def order_filters():
        with db_connection() as db:
            benches = [dict(r) for r in db.execute("SELECT key,label FROM source_filters ORDER BY label")]
            remarks = [r["remarks"] for r in db.execute("SELECT DISTINCT remarks FROM source_orders WHERE remarks!='' ORDER BY remarks")]
            categories = [r['category'] for r in db.execute('SELECT category FROM ibbi_sources ORDER BY category')]
        return {"benches": benches, "remarks": remarks, "categories": categories}

    @app.get("/api/orders/summary")
    def order_summary():
        with db_connection() as db:
            by_year = [dict(r) for r in db.execute('''SELECT substr(order_date,1,4) AS "year",COUNT(*) AS orders
                FROM source_orders WHERE order_date IS NOT NULL
                GROUP BY substr(order_date,1,4) ORDER BY substr(order_date,1,4)''')]
            by_remark = [dict(r) for r in db.execute("""SELECT remarks,COUNT(*) orders FROM source_orders
                GROUP BY remarks ORDER BY orders DESC LIMIT 15""")]
            date_range = db.execute("SELECT MIN(order_date) earliest,MAX(order_date) latest FROM source_orders").fetchone()
        return {"by_year": by_year, "top_remarks": by_remark,
                "earliest": date_range["earliest"], "latest": date_range["latest"]}

    @app.get("/api/orders")
    def list_orders(q: str = Query("", max_length=200), bench: str = "", remark: str = "", adverse: str = "",
                    category: str = Query('',max_length=100),
                    date_from: str = "", date_to: str = "", page: int = Query(1, ge=1),
                    page_size: int = Query(25, ge=1, le=100)):
        if date_from and (len(date_from) != 10 or not date_from[:4].isdigit()):
            raise HTTPException(422, "Invalid start date")
        if date_to and (len(date_to) != 10 or not date_to[:4].isdigit()):
            raise HTTPException(422, "Invalid end date")
        where = ["1=1"]
        args = []
        if category:
            where.append('EXISTS(SELECT 1 FROM order_categories c WHERE c.order_id=source_orders.id AND c.category=?)')
            args.append(category)
        if q:
            where.append("(subject LIKE ? OR remarks LIKE ?)")
            args.extend([f"%{q}%", f"%{q}%"])
        if bench:
            where.append("bench_id=?")
            args.append(bench)
        if remark:
            where.append("remarks=?")
            args.append(remark)
        if adverse:
            if adverse not in {"ip", "other"}:
                raise HTTPException(422, "Invalid adverse filter")
            where.append("EXISTS(SELECT 1 FROM source_order_filters f WHERE f.order_id=source_orders.id AND f.filter_key=?)")
            args.append(f"adverse:{adverse}")
        if date_from:
            where.append("order_date>=?")
            args.append(date_from)
        if date_to:
            where.append("order_date<=?")
            args.append(date_to)
        clause = " AND ".join(where)
        with db_connection() as db:
            total = db.execute(f"SELECT COUNT(*) FROM source_orders WHERE {clause}", args).fetchone()[0]
            rows = [dict(r) for r in db.execute(f"""SELECT id,pdf_url,order_date,subject,remarks,
                bench_id,bench_name,pdf_status,pdf_error,case_id FROM source_orders
                WHERE {clause} ORDER BY order_date DESC,id LIMIT ? OFFSET ?""",
                (*args, page_size, (page-1)*page_size))]
            for row in rows:
                row['categories'] = [r['category'] for r in db.execute('SELECT category FROM order_categories WHERE order_id=? ORDER BY category',(row['id'],))]
        return {"total": total, "page": page, "page_size": page_size, "items": rows}

    @app.post("/api/orders/{order_id}/review")
    def review_order(order_id: str):
        with db_connection() as db:
            row = db.execute("SELECT * FROM source_orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Source order not found")
            if row["case_id"]:
                return {"case_id": row["case_id"], "duplicate": True}
        try:
            path = download_order(storage, order_id)
            result = ingest(path.read_bytes(), path.name, row["pdf_url"])
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(502, f"IBBI PDF could not be downloaded: {exc}") from exc
        with db_connection() as db:
            db.execute("UPDATE source_orders SET case_id=?,document_id=? WHERE id=?", (result["case_id"], result['document_id'], order_id))
        return result

    @app.get("/api/cases")
    def list_cases(q: str = Query("", max_length=200), status: str = "all"):
        with db_connection() as db:
            cases = [decode(r) for r in db.execute("SELECT * FROM cases ORDER BY created_at DESC")]
            subjects = {}
            for row in db.execute("SELECT d.case_id,COALESCE(o.subject,d.subject) subject FROM documents d LEFT JOIN source_orders o ON o.document_id=d.id ORDER BY d.created_at,d.id"):
                if row["subject"]:
                    subjects.setdefault(row["case_id"], []).append(row["subject"])
        result = []
        for case in cases:
            case["subjects"] = subjects.get(case["id"], [])
            record = CaseRecord.model_validate(case["record"])
            search = " ".join([f.value for f in [record.company, record.case_number, record.professional, record.cin] if f] + case["subjects"])
            if q.casefold() not in search.casefold():
                continue
            if status != "all" and case["review_status"] != status:
                continue
            case["metrics"] = metrics(record)
            result.append(case)
        return result

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str):
        with db_connection() as db:
            case = get_case(db, case_id)
            case["documents"] = [decode(r) for r in db.execute("SELECT id,case_id,filename,sha256,source_url,extraction,created_at,subject,order_kind FROM documents WHERE case_id=? ORDER BY created_at", (case_id,))]
            for document in case['documents']:
                source = db.execute('SELECT subject FROM source_orders WHERE document_id=? ORDER BY order_date DESC LIMIT 1', (document['id'],)).fetchone()
                document['pdf_subject'] = document['subject']
                if source:
                    document['subject'] = source['subject']
            case["audit"] = [decode(r) for r in db.execute("SELECT * FROM audit WHERE case_id=? ORDER BY id DESC", (case_id,))]
        case["metrics"] = metrics(CaseRecord.model_validate(case["record"]))
        return case

    @app.get("/api/documents/{doc_id}/pdf")
    def document_pdf(doc_id: str):
        with db_connection() as db:
            row = db.execute("SELECT id,content FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Document not found")
            if row["content"] is not None:
                return Response(bytes(row["content"]), media_type="application/pdf")
        return FileResponse(storage / "documents" / f"{row['id']}.pdf", media_type="application/pdf")

    @app.put("/api/cases/{case_id}")
    def save(case_id: str, payload: SaveRecord):
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            case = get_case(db, case_id)
            if payload.version != case["version"]:
                raise HTTPException(409, "Case changed; reload before saving")
            validate_evidence(db, case_id, payload.record)
            updated = db.execute("UPDATE cases SET record=?,version=version+1,review_status='draft',reviewed_by=NULL,reviewed_at=NULL WHERE id=? AND version=?",
                       (payload.record.model_dump_json(), case_id, payload.version))
            if updated.rowcount != 1:
                raise HTTPException(409, "Case changed; reload before saving")
            audit(db, case_id, "saved_draft", payload.record.model_dump(mode="json"))
            project(db, case_id, payload.record.model_dump(mode="json"))
        return case_detail(case_id)

    @app.post("/api/cases/{case_id}/approve")
    def approve(case_id: str, payload: Approval):
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            case = get_case(db, case_id)
            if payload.version != case["version"]:
                raise HTTPException(409, "Case changed; reload before approving")
            record = CaseRecord.model_validate(case["record"])
            if not payload.confirmed or not record.company or not record.case_number:
                raise HTTPException(422, "Review confirmation, company and case number are required")
            if record.outcome != "unknown" and not record.outcome_evidence:
                raise HTTPException(422, "The outcome needs page evidence")
            validate_evidence(db, case_id, record)
            updated = db.execute("UPDATE cases SET review_status='approved',version=version+1,reviewed_by=?,reviewed_at=? WHERE id=? AND version=?",
                       (payload.reviewer, now(), case_id, payload.version))
            if updated.rowcount != 1:
                raise HTTPException(409, "Case changed; reload before approving")
            audit(db, case_id, "approved", record.model_dump(mode="json"), payload.reviewer)
        return case_detail(case_id)

    @app.get("/api/analytics")
    def analytics(years: int = Query(10, ge=0, le=20)):
        cases = list_cases(q="", status="all")
        approved = [c for c in cases if c["review_status"] == "approved"]
        recent, period = select_recent_cases(approved,years)
        # Only resolution-approved records have a resolution-plan haircut.
        eligible = [c for c in recent if c["record"]["outcome"] == "resolution_approved"]
        admitted = sum((Decimal(c["metrics"]["matched_admitted_inr"]) for c in eligible), Decimal(0))
        planned = sum((Decimal(c["metrics"]["matched_plan_inr"]) for c in eligible), Decimal(0))
        return {**research_analytics(recent), **case_profiles(approved), "period":period,
                "total_cases": len(cases), "approved_cases": len(approved), "draft_cases": len(cases)-len(approved),
                "covered_cases": sum(c["metrics"]["matched_rows"] > 0 for c in eligible),
                "matched_admitted_inr": str(admitted), "matched_plan_inr": str(planned),
                "weighted_plan_haircut_percent": str(((admitted-planned)/admitted*100).quantize(Decimal("0.01"))) if admitted else None,
                "outcomes": {s: sum(c["record"]["outcome"] == s for c in recent) for s in
                             ["unknown", "ongoing", "resolution_approved", "liquidation_ordered", "liquidation_completed"]}}

    app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
    return app


app = create_app()

