import hashlib
from urllib.parse import urlparse
from psycopg import sql
import json
import os
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from dotenv import load_dotenv
import psycopg
import re
from uuid import uuid4
from difflib import SequenceMatcher
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
from .models import CaseRecord
from .document_matching import DocumentMatcher
from .config import DEFAULT_CATEGORIES, IBBI_BASE
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env', override=False)

class Database:
    """Own persistence for both database engines and all maintenance commands.

    A configured instance opens sessions; each session owns one connection and
    commits on success or rolls back on failure. Named read operations return
    materialized rows, and named writes return their affected-row counts.
    """

    def __init__(self, root, database_url=None, *, handle=None, postgres=False):
        self.root = Path(root)
        self.database_url = os.environ.get('DATABASE_URL', '') if database_url is None else database_url
        self._handle = handle
        self.is_postgres = postgres
    DATABASE_ERRORS = (sqlite3.DatabaseError, psycopg.Error)
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.errors.UniqueViolation)

    @contextmanager
    def session(self):
        with self.connection(self.root, self.database_url) as database:
            yield database

    def execute(self, query, params=()):
        if self._handle is None:
            raise RuntimeError('Database operations require an active session')
        if not self.is_postgres:
            return self._handle.execute(query, params)
        sql = query.strip()
        if sql.upper() == 'BEGIN IMMEDIATE':
            return None
        if sql.upper().startswith('INSERT OR IGNORE INTO '):
            sql = 'INSERT INTO ' + sql[len('INSERT OR IGNORE INTO '):] + ' ON CONFLICT DO NOTHING'
        return self._handle.execute(sql.replace('?', '%s'), params)

    def executescript(self, script):
        return self._handle.executescript(script)

    def begin(self):
        self.execute('BEGIN IMMEDIATE')

    class Row(Mapping):
        """Support both row['name'] and row[0] used by the existing API."""

        def __init__(self, names, values):
            self.names = names
            self.values = values
            self.data = dict(zip(names, values))

        def __getitem__(self, key):
            return self.values[key] if isinstance(key, int) else self.data[key]

        def __iter__(self):
            return iter(self.names)

        def __len__(self):
            return len(self.names)

    @staticmethod
    def flex_row(cursor):
        if cursor.description is None:
            return lambda values: values
        names = [column.name for column in cursor.description]
        return lambda values: Database.Row(names, values)

    @staticmethod
    @contextmanager
    def connection(root, database_url=None):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        url = os.environ.get('DATABASE_URL', '') if database_url is None else database_url
        if url:
            with psycopg.connect(url, row_factory=Database.flex_row, connect_timeout=10) as handle:
                yield Database(root, url, handle=handle, postgres=True)
        else:
            handle = sqlite3.connect(root / 'ibc.sqlite3', timeout=20)
            handle.row_factory = sqlite3.Row
            handle.execute('PRAGMA foreign_keys=ON')
            try:
                yield Database(root, '', handle=handle)
                handle.commit()
            except Exception:
                handle.rollback()
                raise
            finally:
                handle.close()
    SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY, record TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    review_status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
    reviewed_by TEXT, reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
    filename TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, source_url TEXT,
    extraction TEXT NOT NULL, created_at TEXT NOT NULL, content BLOB
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
    action TEXT NOT NULL, snapshot TEXT NOT NULL, actor TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_source_evidence (
    document_id TEXT PRIMARY KEY REFERENCES documents(id),
    sha256 TEXT NOT NULL, reader_version TEXT NOT NULL, content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_orders (
    id TEXT PRIMARY KEY, pdf_url TEXT NOT NULL UNIQUE,
    order_date TEXT, subject TEXT NOT NULL, remarks TEXT NOT NULL,
    bench_id TEXT, bench_name TEXT, first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL, pdf_status TEXT NOT NULL DEFAULT 'pending',
    pdf_error TEXT, pdf_sha256 TEXT, pdf_bytes INTEGER,
    pdf_prefix_bytes INTEGER, pdf_fetched_at TEXT,
    case_id TEXT REFERENCES cases(id)
);
CREATE INDEX IF NOT EXISTS source_orders_date_idx ON source_orders(order_date DESC);
CREATE INDEX IF NOT EXISTS source_orders_status_idx ON source_orders(pdf_status);
CREATE TABLE IF NOT EXISTS source_pages (
    filter_key TEXT NOT NULL, page_number INTEGER NOT NULL,
    fetched_at TEXT NOT NULL, row_count INTEGER NOT NULL,
    last_page INTEGER NOT NULL, PRIMARY KEY(filter_key, page_number)
);
CREATE TABLE IF NOT EXISTS source_page_failures (
    filter_key TEXT NOT NULL, page_number INTEGER NOT NULL,
    last_error TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
    last_attempt_at TEXT NOT NULL, PRIMARY KEY(filter_key,page_number)
);
CREATE TABLE IF NOT EXISTS source_filters (
    key TEXT PRIMARY KEY, label TEXT NOT NULL, first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_order_filters (
    order_id TEXT NOT NULL REFERENCES source_orders(id), filter_key TEXT NOT NULL,
    PRIMARY KEY(order_id,filter_key)
);
"""
    POSTGRES_SCHEMA = SQLITE_SCHEMA.replace('content BLOB', 'content BYTEA').replace(
        'id INTEGER PRIMARY KEY, case_id',
        'id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, case_id',
    )
    STRUCTURED_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 kind TEXT NOT NULL, identifier TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS entity_name_idx ON entities(normalized_name);
CREATE TABLE IF NOT EXISTS case_details (
 case_id TEXT PRIMARY KEY REFERENCES cases(id), company TEXT, cin TEXT,
 company_entity_id TEXT REFERENCES entities(id), nclt_bench TEXT,
 admission_date TEXT, resolution_date TEXT, irp TEXT, rp TEXT, outcome TEXT NOT NULL,
 registered_office TEXT, industry TEXT, applicant TEXT, applicant_cin TEXT,
 fair_value NUMERIC(20,2), liquidation_value NUMERIC(20,2)
);
CREATE INDEX IF NOT EXISTS case_cin_idx ON case_details(cin);
CREATE TABLE IF NOT EXISTS claims (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 creditor_name TEXT NOT NULL, creditor_type TEXT NOT NULL, scope TEXT NOT NULL,
 filed NUMERIC(20,2), admitted NUMERIC(20,2), plan_amount NUMERIC(20,2),
 actual_paid NUMERIC(20,2), document_id TEXT NOT NULL REFERENCES documents(id), page INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS claims_case_idx ON claims(case_id);
CREATE TABLE IF NOT EXISTS people (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 entity_id TEXT NOT NULL REFERENCES entities(id), name TEXT NOT NULL,
 role TEXT NOT NULL, period TEXT NOT NULL, din TEXT, cin TEXT,
 document_id TEXT NOT NULL REFERENCES documents(id), page INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS people_case_idx ON people(case_id);
CREATE TABLE IF NOT EXISTS plans (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), applicant TEXT,
 amount NUMERIC(20,2), admitted NUMERIC(20,2), haircut NUMERIC(8,2),
 document_id TEXT NOT NULL REFERENCES documents(id), page INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS plans_case_idx ON plans(case_id);
CREATE TABLE IF NOT EXISTS import_jobs (
 id TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_files (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES import_jobs(id),
 document_id TEXT NOT NULL REFERENCES documents(id), source_url TEXT NOT NULL,
 duplicate INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(job_id,source_url)
);
CREATE INDEX IF NOT EXISTS import_files_document_idx ON import_files(document_id);
CREATE INDEX IF NOT EXISTS import_files_source_idx ON import_files(source_url,created_at);
"""

    def entity(self, name, kind, identifier=None):
        if identifier:
            row = self.execute('SELECT id FROM entities WHERE identifier=?', (identifier,)).fetchone()
            if row:
                return row['id']
        key = uuid4().hex
        self.execute(
            'INSERT INTO entities(id,name,normalized_name,kind,identifier) VALUES(?,?,?,?,?)',
            (key, name, DocumentMatcher.normalized(name), kind, identifier),
        )
        return key

    def project(self, case_id, record):
        r = CaseRecord.model_validate(record)
        value = lambda f: f.value if f else None
        for table in ('case_details', 'claims', 'people', 'plans'):
            self.execute(f'DELETE FROM {table} WHERE case_id=?', (case_id,))
        company_id = self.entity(r.company.value, 'company', 'CIN:' + r.cin.value.upper() if r.cin else None) if r.company else None
        self.execute(
            """INSERT INTO case_details
        (case_id,company,cin,company_entity_id,nclt_bench,admission_date,resolution_date,irp,rp,outcome,
         registered_office,industry,applicant,applicant_cin,fair_value,liquidation_value)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                value(r.company),
                value(r.cin),
                company_id,
                value(r.tribunal),
                value(r.admission_date),
                value(r.resolution_date),
                value(r.irp),
                value(r.professional),
                r.outcome,
                value(r.registered_office),
                value(r.industry),
                value(r.applicant),
                value(r.applicant_cin),
                value(r.fair_value),
                value(r.liquidation_value),
            ),
        )
        for c in r.claims:
            self.execute(
                'INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (
                    uuid4().hex,
                    case_id,
                    c.creditor,
                    c.category,
                    c.scope,
                    str(c.claimed) if c.claimed is not None else None,
                    str(c.admitted) if c.admitted is not None else None,
                    str(c.plan_amount) if c.plan_amount is not None else None,
                    str(c.actual_paid) if c.actual_paid is not None else None,
                    c.document_id,
                    c.page,
                ),
            )
        for field, role, period in (
            ('previous_owners', 'owner', 'before'),
            ('current_owners', 'owner', 'current'),
            ('subsequent_owners', 'owner', 'after'),
            ('previous_directors', 'director', 'before'),
            ('current_directors', 'director', 'current'),
            ('subsequent_directors', 'director', 'after'),
        ):
            for f in getattr(r, field):
                identifier = 'DIN:' + f.din if f.din else 'CIN:' + f.cin if f.cin else None
                eid = self.entity(f.value, 'company' if f.cin else 'person', identifier)
                self.execute(
                    'INSERT INTO people VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (
                        uuid4().hex,
                        case_id,
                        eid,
                        f.value,
                        role,
                        period,
                        f.din,
                        f.cin,
                        f.document_id,
                        f.page,
                    ),
                )
        for p in r.plans:
            haircut = (p.admitted - p.amount) / p.admitted * 100 if p.admitted and p.amount is not None else None
            self.execute(
                'INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)',
                (
                    uuid4().hex,
                    case_id,
                    p.applicant,
                    str(p.amount) if p.amount is not None else None,
                    str(p.admitted) if p.admitted is not None else None,
                    str(round(haircut, 2)) if haircut is not None else None,
                    p.document_id,
                    p.page,
                ),
            )
        self.execute(
            """DELETE FROM entities WHERE identifier IS NULL
        AND id NOT IN (SELECT entity_id FROM people)
        AND id NOT IN (SELECT company_entity_id FROM case_details WHERE company_entity_id IS NOT NULL)""",
        )

    def initialize_structured(self):
        for statement in self.STRUCTURED_SCHEMA.split(';'):
            if statement.strip():
                self.execute(statement)
        extra_columns = {
            'registered_office': 'TEXT',
            'industry': 'TEXT',
            'applicant': 'TEXT',
            'applicant_cin': 'TEXT',
            'fair_value': 'NUMERIC(20,2)',
            'liquidation_value': 'NUMERIC(20,2)',
        }
        if self.is_postgres:
            for column, kind in extra_columns.items():
                self.execute(f'ALTER TABLE case_details ADD COLUMN IF NOT EXISTS {column} {kind}')
        else:
            existing = {row['name'] for row in self.execute('PRAGMA table_info(case_details)')}
            for column, kind in extra_columns.items():
                if column not in existing:
                    self.execute(f'ALTER TABLE case_details ADD COLUMN {column} {kind}')
        for row in self.execute(
            'SELECT id,record FROM cases WHERE id NOT IN (SELECT case_id FROM case_details)',
        ).fetchall():
            self.project(row['id'], json.loads(row['record']))
        for job in self.execute('SELECT id,created_at,result FROM import_jobs').fetchall():
            self.persist_import_files(job['id'], json.loads(job['result']), job['created_at'])

    def persist_import_files(self, job_id, result, timestamp):
        for item in result.get('items', []):
            doc_id = item.get('document_id')
            if not doc_id:
                docs = self.execute(
                    'SELECT id,source_url FROM documents WHERE case_id=?',
                    (item['case_id'],),
                ).fetchall()
                matches = [d for d in docs if d['source_url'] == item['url']]
                doc_id = matches[0]['id'] if len(matches) == 1 else None
            if doc_id:
                self.execute(
                    'INSERT OR IGNORE INTO import_files VALUES(?,?,?,?,?,?)',
                    (uuid4().hex, job_id, doc_id, item['url'], int(item['duplicate']), timestamp),
                )

    def match_candidates(self, name, din=None, cin=None):
        identifier = 'DIN:' + din if din else 'CIN:' + cin if cin else None
        if identifier:
            return [{**dict(r), 'match': 'identifier', 'score': 1.0} for r in self.execute('SELECT * FROM entities WHERE identifier=?', (identifier,))]
        candidates = []
        for row in self.execute('SELECT * FROM entities'):
            score = SequenceMatcher(None, DocumentMatcher.normalized(name), row['normalized_name']).ratio()
            if score >= 0.8:
                candidates.append(
                    {
                        **dict(row),
                        'match': 'name_candidate_requires_review',
                        'score': round(score, 3),
                    },
                )
        return sorted(candidates, key=lambda c: c['score'], reverse=True)[:20]
    AUTOMATIC_SCHEMA = """
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

    def initialize_automatic(self):
        for statement in self.AUTOMATIC_SCHEMA.split(';'):
            if statement.strip():
                self.execute(statement)
        if self.is_postgres:
            self.execute(
                "ALTER TABLE scheduler_settings ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'all'",
            )
            self.execute('ALTER TABLE ibbi_sources ADD COLUMN IF NOT EXISTS created_at TEXT')
        elif 'category' not in {r['name'] for r in self.execute('PRAGMA table_info(scheduler_settings)')}:
            self.execute(
                "ALTER TABLE scheduler_settings ADD COLUMN category TEXT NOT NULL DEFAULT 'all'",
            )
        if not self.is_postgres and 'created_at' not in {r['name'] for r in self.execute('PRAGMA table_info(ibbi_sources)')}:
            self.execute('ALTER TABLE ibbi_sources ADD COLUMN created_at TEXT')
        self.execute('INSERT OR IGNORE INTO scheduler_settings(id) VALUES(1)')
        for category in DEFAULT_CATEGORIES:
            self.execute(
                'INSERT OR IGNORE INTO ibbi_sources(category,url,created_at) VALUES(?,?,?)',
                (category, f'{IBBI_BASE}/orders/{category}', self.timestamp()),
            )
        for row in self.execute(
            """SELECT DISTINCT d.id,c.category FROM documents d
        JOIN source_orders o ON o.pdf_url=d.source_url JOIN order_categories c ON c.order_id=o.id
        UNION SELECT DISTINCT f.document_id,c.category FROM import_files f
        JOIN source_orders o ON o.pdf_url=f.source_url JOIN order_categories c ON c.order_id=o.id""",
        ).fetchall():
            self.set_document_category(row[0], row[1])

    def set_document_category(self, document_id, category):
        self.execute(
            'INSERT OR IGNORE INTO document_categories VALUES(?,?)',
            (document_id, category),
        )
        self.execute(
            (
                'UPDATE documents SET order_kind=? WHERE id=? AND (order_kind IS NULL OR '
                "order_kind='unknown')"
            ),
            (category, document_id),
        )
    MATCHING_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_fingerprints (
 document_id TEXT PRIMARY KEY REFERENCES documents(id), text_hash TEXT,
 normalized_name TEXT NOT NULL, normalized_subject TEXT NOT NULL, ingest_day TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fingerprint_text_idx ON document_fingerprints(text_hash);
CREATE INDEX IF NOT EXISTS fingerprint_name_idx ON document_fingerprints(normalized_name);
CREATE INDEX IF NOT EXISTS fingerprint_subject_idx ON document_fingerprints(normalized_subject);
CREATE TABLE IF NOT EXISTS document_identifiers (
 document_id TEXT NOT NULL REFERENCES documents(id), kind TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(document_id,kind,value)
);
CREATE INDEX IF NOT EXISTS document_identifier_value_idx ON document_identifiers(kind,value);
CREATE TABLE IF NOT EXISTS document_matches (
 document_id TEXT NOT NULL REFERENCES documents(id), matched_document_id TEXT NOT NULL REFERENCES documents(id),
 reasons TEXT NOT NULL, PRIMARY KEY(document_id,matched_document_id)
);
"""

    def initialize_matching(self):
        for statement in self.MATCHING_SCHEMA.split(';'):
            if statement.strip():
                self.execute(statement)
        rows = self.execute(
            (
                'SELECT id,filename,extraction,created_at FROM documents WHERE id NOT IN '
                '(SELECT document_id FROM document_fingerprints)'
            ),
        ).fetchall()
        for row in rows:
            DocumentMatcher.fingerprint(
                self,
                row['id'],
                row['filename'],
                json.loads(row['extraction']),
                row['created_at'],
            )
    ENTRIES = """
SELECT o.id AS id, o.id AS source_order_id, o.pdf_url AS official_pdf_url,
       o.order_date, o.subject, o.remarks, o.bench_id, o.bench_name,
       CASE WHEN d.id IS NOT NULL THEN 'downloaded' ELSE o.pdf_status END AS pdf_status,
       o.pdf_error, o.first_seen_at AS created_at,
       o.pdf_fetched_at AS imported_at, o.document_id,
       COALESCE(d.case_id,o.case_id) AS case_id, d.filename,
       o.pdf_url AS source_url, COALESCE(f.duplicate,0) AS duplicate,
       f.job_id, d.source_url AS stored_source_url,
       d.created_at AS document_created_at, d.sha256, d.order_kind,
       d.subject AS pdf_subject
FROM source_orders o
LEFT JOIN documents d ON d.id=o.document_id
LEFT JOIN import_files f ON f.id=(
    SELECT fi.id FROM import_files fi WHERE fi.source_url=o.pdf_url AND fi.document_id=o.document_id
    ORDER BY fi.created_at DESC,fi.id DESC LIMIT 1)
UNION ALL
SELECT f.id,NULL,NULL,NULL,d.subject,'',NULL,NULL,'downloaded',NULL,
       f.created_at,f.created_at,d.id,d.case_id,d.filename,
       f.source_url,f.duplicate,f.job_id,d.source_url,d.created_at,
       d.sha256,d.order_kind,d.subject
FROM import_files f JOIN documents d ON d.id=f.document_id
WHERE NOT EXISTS(SELECT 1 FROM source_orders o WHERE o.pdf_url=f.source_url AND o.document_id=f.document_id)
UNION ALL
SELECT d.id,NULL,NULL,NULL,d.subject,'',NULL,NULL,'downloaded',NULL,
       d.created_at,d.created_at,d.id,d.case_id,d.filename,
       d.source_url,0,NULL,d.source_url,d.created_at,d.sha256,
       d.order_kind,d.subject
FROM documents d
WHERE NOT EXISTS(SELECT 1 FROM import_files f WHERE f.document_id=d.id)
  AND NOT EXISTS(SELECT 1 FROM source_orders o WHERE o.document_id=d.id)
"""

    def catalogue_entries(
        self,
        *,
        page=1,
        page_size=25,
        q='',
        category='',
        bench='',
        remark='',
        adverse='',
        date_from='',
        date_to='',
        duplicate='all',
    ):
        where, args = (['1=1'], [])
        if q:
            where.append(
                (
                    "(LOWER(COALESCE(e.subject,'')) LIKE ? OR LOWER(COALESCE(e.remarks,'')) LIKE "
                    "? OR LOWER(COALESCE(e.filename,'')) LIKE ? OR "
                    "LOWER(COALESCE(e.source_url,'')) LIKE ?)"
                ),
            )
            args.extend([f'%{q.casefold()}%'] * 4)
        if category:
            where.append(
                """(EXISTS(SELECT 1 FROM order_categories c WHERE c.order_id=e.source_order_id AND c.category=?)
                        OR (e.source_order_id IS NULL AND EXISTS(SELECT 1 FROM document_categories c WHERE c.document_id=e.document_id AND c.category=?)))""",
            )
            args.extend([category, category])
        if bench:
            where.append('e.bench_id=?')
            args.append(bench)
        if remark:
            where.append('e.remarks=?')
            args.append(remark)
        if adverse:
            where.append(
                (
                    'EXISTS(SELECT 1 FROM source_order_filters f WHERE '
                    'f.order_id=e.source_order_id AND f.filter_key=?)'
                ),
            )
            args.append(f'adverse:{adverse}')
        if date_from:
            where.append(
                'COALESCE(e.order_date,SUBSTR(e.imported_at,1,10),SUBSTR(e.created_at,1,10))>=?',
            )
            args.append(date_from)
        if date_to:
            where.append(
                'COALESCE(e.order_date,SUBSTR(e.imported_at,1,10),SUBSTR(e.created_at,1,10))<=?',
            )
            args.append(date_to)
        if duplicate != 'all':
            where.append('e.document_id IS NOT NULL AND e.duplicate=?')
            args.append(int(duplicate == 'yes'))
        filtered = 'FROM ibbi_order_catalogue e WHERE ' + ' AND '.join(where)
        total = self.execute('SELECT COUNT(*) ' + filtered, args).fetchone()[0]
        rows = [dict(row) for row in self.execute(
            'SELECT * ' + filtered + """ ORDER BY
        COALESCE(order_date,SUBSTR(imported_at,1,10),SUBSTR(created_at,1,10)) DESC,
        created_at DESC,id LIMIT ? OFFSET ?""",
            (*args, page_size, (page - 1) * page_size),
        )]
        for row in rows:
            if row['source_order_id']:
                row['categories'] = [r['category'] for r in self.execute(
                    'SELECT category FROM order_categories WHERE order_id=? ORDER BY category',
                    (row['source_order_id'],),
                )]
            else:
                row['categories'] = [r['category'] for r in self.execute(
                    'SELECT category FROM document_categories WHERE document_id=? ORDER BY category',
                    (row['document_id'],),
                )]
            row['imported_filename'] = unquote(urlsplit(row['source_url'] or '').path.rsplit('/', 1)[-1]) or row['filename']
            if row['document_id']:
                row['possible_matches'] = DocumentMatcher.candidates(self, row['document_id'])
                row['duplicate_of'] = {
                    'document_id': row['document_id'],
                    'filename': row['filename'],
                    'source_url': row['stored_source_url'],
                    'created_at': row['document_created_at'],
                } if row['duplicate'] else None
            else:
                row['possible_matches'] = []
                row['duplicate_of'] = None
        return {'items': rows, 'total': total, 'page': page, 'page_size': page_size}

    @staticmethod
    def timestamp():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def decode(row):
        result = dict(row)
        result.pop('content', None)
        for key in ('record', 'extraction', 'snapshot'):
            if key in result and isinstance(result[key], str):
                result[key] = json.loads(result[key])
        return result

    def initialize(self):
        root = self.root
        database_url = self.database_url
        (root / 'documents').mkdir(parents=True, exist_ok=True)
        url = os.environ.get('DATABASE_URL', '') if database_url is None else database_url
        if url:
            with self.connection(root, database_url=url) as db:
                for statement in self.POSTGRES_SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
                db.execute('ALTER TABLE documents ADD COLUMN IF NOT EXISTS content BYTEA')
                db.execute('ALTER TABLE documents ADD COLUMN IF NOT EXISTS subject TEXT')
                db.execute(
                    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS order_kind TEXT DEFAULT 'unknown'",
                )
                db.execute(
                    'ALTER TABLE source_orders ADD COLUMN IF NOT EXISTS document_id TEXT REFERENCES documents(id)',
                )
        else:
            with self.connection(root, database_url='') as db:
                db.executescript(self.SQLITE_SCHEMA)
                fields = {row['name'] for row in db.execute('PRAGMA table_info(documents)')}
                if 'content' not in fields:
                    db.execute('ALTER TABLE documents ADD COLUMN content BLOB')
                if 'subject' not in fields:
                    db.execute('ALTER TABLE documents ADD COLUMN subject TEXT')
                if 'order_kind' not in fields:
                    db.execute("ALTER TABLE documents ADD COLUMN order_kind TEXT DEFAULT 'unknown'")
                order_fields = {row['name'] for row in db.execute('PRAGMA table_info(source_orders)')}
                if 'document_id' not in order_fields:
                    db.execute(
                        'ALTER TABLE source_orders ADD COLUMN document_id TEXT REFERENCES documents(id)',
                    )
        with self.connection(root, database_url=url) as db:
            db.initialize_structured()
            for order in db.execute('SELECT id,pdf_url FROM source_orders WHERE document_id IS NULL').fetchall():
                matches = db.execute(
                    (
                        'SELECT id FROM documents WHERE source_url=? UNION SELECT document_id FROM '
                        'import_files WHERE source_url=?'
                    ),
                    (order['pdf_url'], order['pdf_url']),
                ).fetchall()
                if len(matches) == 1:
                    db.execute(
                        'UPDATE source_orders SET document_id=? WHERE id=?',
                        (matches[0][0], order['id']),
                    )
            db.initialize_automatic()
            db.initialize_matching()
            if url:
                db.execute('CREATE OR REPLACE VIEW ibbi_order_catalogue AS ' + self.ENTRIES)
            else:
                db.execute('CREATE VIEW IF NOT EXISTS ibbi_order_catalogue AS ' + self.ENTRIES)

    def list_import_files(self, page: int, page_size: int, q: str, duplicate: str, job_id: str):
        source = """SELECT f.id,f.job_id,f.document_id,d.case_id,d.filename,f.source_url,
                    f.duplicate,f.created_at,d.sha256,d.source_url AS stored_source_url,d.created_at AS document_created_at,
                    COALESCE(o.subject,d.subject) subject,d.order_kind,o.order_date,o.id source_order_id,d.subject pdf_subject
                    FROM import_files f JOIN documents d ON d.id=f.document_id LEFT JOIN source_orders o ON o.pdf_url=f.source_url
                    UNION ALL SELECT d.id,NULL,d.id,d.case_id,d.filename,d.source_url,0,d.created_at,d.sha256,d.source_url,d.created_at,
                    COALESCE(o.subject,d.subject),d.order_kind,o.order_date,o.id,d.subject
                    FROM documents d LEFT JOIN source_orders o ON o.pdf_url=d.source_url WHERE NOT EXISTS(SELECT 1 FROM import_files f WHERE f.document_id=d.id)"""
        where, params = (
            [(
                "(LOWER(filename) LIKE ? OR LOWER(COALESCE(source_url,'')) LIKE ? OR "
                "LOWER(COALESCE(subject,'')) LIKE ?)"
            )],
            [f'%{q.lower()}%'] * 3,
        )
        if duplicate != 'all':
            where.append('duplicate=?')
            params.append(int(duplicate == 'yes'))
        if job_id:
            where.append('job_id=?')
            params.append(job_id)
        filtered = f'FROM ({source}) AS files WHERE ' + ' AND '.join(where)
        count = self.execute('SELECT COUNT(*) ' + filtered, params).fetchone()[0]
        ordering = 'CASE WHEN order_date IS NULL THEN 1 ELSE 0 END,order_date DESC,created_at ASC,id' if job_id else 'created_at DESC,id'
        rows = [dict(r) for r in self.execute(
            'SELECT * ' + filtered + f' ORDER BY {ordering} LIMIT ? OFFSET ?',
            (*params, page_size, (page - 1) * page_size),
        )]
        for row in rows:
            row['possible_matches'] = DocumentMatcher.candidates(self, row['document_id'])
            row['categories'] = [r['category'] for r in self.execute(
                'SELECT category FROM document_categories WHERE document_id=? ORDER BY category',
                (row['document_id'],),
            )]
        return (count, rows)

    def list_source_orders(
        self,
        q: str,
        bench: str,
        remark: str,
        adverse: str,
        category: str,
        date_from: str,
        date_to: str,
        page: int,
        page_size: int,
    ):
        where = ['1=1']
        args = []
        if category:
            where.append(
                'EXISTS(SELECT 1 FROM order_categories c WHERE c.order_id=source_orders.id AND c.category=?)',
            )
            args.append(category)
        if q:
            where.append('(subject LIKE ? OR remarks LIKE ?)')
            args.extend([f'%{q}%', f'%{q}%'])
        if bench:
            where.append('bench_id=?')
            args.append(bench)
        if remark:
            where.append('remarks=?')
            args.append(remark)
        if adverse:
            if adverse not in {'ip', 'other'}:
                raise ValueError('Invalid adverse filter')
            where.append(
                (
                    'EXISTS(SELECT 1 FROM source_order_filters f WHERE '
                    'f.order_id=source_orders.id AND f.filter_key=?)'
                ),
            )
            args.append(f'adverse:{adverse}')
        if date_from:
            where.append('order_date>=?')
            args.append(date_from)
        if date_to:
            where.append('order_date<=?')
            args.append(date_to)
        clause = ' AND '.join(where)
        total = self.execute(f'SELECT COUNT(*) FROM source_orders WHERE {clause}', args).fetchone()[0]
        rows = [dict(r) for r in self.execute(
            f'SELECT id,pdf_url,order_date,subject,remarks,\n                bench_id,bench_name,pdf_status,pdf_error,case_id FROM source_orders\n                WHERE {clause} ORDER BY order_date DESC,id LIMIT ? OFFSET ?',
            (*args, page_size, (page - 1) * page_size),
        )]
        for row in rows:
            row['categories'] = [r['category'] for r in self.execute(
                'SELECT category FROM order_categories WHERE order_id=? ORDER BY category',
                (row['id'],),
            )]
        return (total, rows)

    def claim_scheduler_lease(self, until, job_id, current, due_only):
        query = (
            'UPDATE scheduler_settings SET lease_until=?,active_job=? WHERE id=1 AND '
            '(lease_until IS NULL OR lease_until<?)'
        )
        args = [until, job_id, current]
        if due_only:
            query += ' AND enabled=1 AND (next_run IS NULL OR next_run<=?)'
            args.append(current)
        return self.execute(query, args).rowcount

    def pending_pdf_ids(self, retry_failed, limit):
        status_filter = "pdf_status IN ('pending','failed')" if retry_failed else "pdf_status='pending'"
        rows = self.execute(
            f'SELECT id FROM source_orders WHERE {status_filter} ORDER BY order_date DESC LIMIT ?',
            (limit or 1000000,),
        )
        return [row['id'] for row in rows]

    def scheduler_settings(self):
        return self.execute('SELECT * FROM scheduler_settings WHERE id=1', ()).fetchone()

    def all_ibbi_sources(self):
        return list(self.execute('SELECT * FROM ibbi_sources ORDER BY category', ()))

    def configure_scheduler(self, enabled, interval_minutes, next_run, category):
        return self.execute(
            'UPDATE scheduler_settings SET enabled=?,interval_minutes=?,next_run=?,category=? WHERE id=1',
            (enabled, interval_minutes, next_run, category),
        ).rowcount

    def ibbi_category(self, category):
        return self.execute('SELECT category FROM ibbi_sources WHERE category=?', (category,)).fetchone()

    def scheduler_active_job(self):
        return self.execute('SELECT active_job FROM scheduler_settings WHERE id=1', ()).fetchone()

    def interrupt_import_job(self, old_job):
        return self.execute(
            "UPDATE import_jobs SET status='interrupted' WHERE id=? AND status IN ('queued','running')",
            (old_job,),
        ).rowcount

    def scheduler_category(self):
        return self.execute('SELECT category FROM scheduler_settings WHERE id=1', ()).fetchone()

    def insert_import_job(self, job_id, url, status, created_at, updated_at, result_json):
        return self.execute(
            'INSERT INTO import_jobs VALUES(?,?,?,?,?,?)',
            (job_id, url, status, created_at, updated_at, result_json),
        ).rowcount

    def scheduler_lease(self):
        return self.execute('SELECT lease_until FROM scheduler_settings WHERE id=1', ()).fetchone()

    def schedule_next_run(self, timestamp):
        return self.execute('UPDATE scheduler_settings SET next_run=? WHERE id=1', (timestamp,)).rowcount

    def update_import_progress(self, status, record_json, timestamp, job_id):
        return self.execute(
            'UPDATE import_jobs SET status=?,result=?,updated_at=? WHERE id=?',
            (status, record_json, timestamp, job_id),
        ).rowcount

    def renew_scheduler_lease(self, lease, job_id):
        return self.execute(
            'UPDATE scheduler_settings SET lease_until=? WHERE id=1 AND active_job=?',
            (lease, job_id),
        ).rowcount

    def register_ibbi_source(self, category, url, created_at):
        return self.execute(
            'INSERT OR IGNORE INTO ibbi_sources(category,url,created_at) VALUES(?,?,?)',
            (category, url, created_at),
        ).rowcount

    def import_job_result(self, job_id):
        return self.execute('SELECT result FROM import_jobs WHERE id=?', (job_id,)).fetchone()

    def selected_ibbi_sources(self, selected):
        return list(
            self.execute(
                'SELECT * FROM ibbi_sources ORDER BY category' if selected == 'all' else 'SELECT * FROM ibbi_sources WHERE category=? ORDER BY category',
                () if selected == 'all' else (selected,),
            ),
        )

    def scheduler_interval(self):
        return self.execute('SELECT interval_minutes FROM scheduler_settings WHERE id=1', ()).fetchone()

    def release_scheduler_lease(self, next_run, job_id):
        return self.execute(
            (
                'UPDATE scheduler_settings SET lease_until=NULL,active_job=NULL,next_run=? '
                'WHERE id=1 AND active_job=?'
            ),
            (next_run, job_id),
        ).rowcount

    def start_source_sync(self, started, category):
        return self.execute(
            "UPDATE ibbi_sources SET status='running',last_attempt=?,error=NULL WHERE category=?",
            (started, category),
        ).rowcount

    def upsert_source_filter(self, bench_id, label, started, timestamp):
        return self.execute(
            """INSERT INTO source_filters VALUES(?,?,?,?) ON CONFLICT(key)
                            DO UPDATE SET label=excluded.label,last_seen_at=excluded.last_seen_at""",
            (bench_id, label, started, timestamp),
        ).rowcount

    def source_order_import_state(self, pdf_url):
        return self.execute(
            (
                'SELECT case_id,document_id,pdf_status,pdf_error,subject,remarks,order_date '
                'FROM source_orders WHERE pdf_url=?'
            ),
            (pdf_url,),
        ).fetchone()

    def upsert_source_order(self, id, pdf_url, order_date, subject, remarks, started, timestamp):
        return self.execute(
            """INSERT INTO source_orders(id,pdf_url,order_date,subject,remarks,first_seen_at,last_seen_at)
                            VALUES(?,?,?,?,?,?,?) ON CONFLICT(pdf_url) DO UPDATE SET order_date=excluded.order_date,
                            subject=excluded.subject,remarks=excluded.remarks,last_seen_at=excluded.last_seen_at""",
            (id, pdf_url, order_date, subject, remarks, started, timestamp),
        ).rowcount

    def add_order_category(self, id, category):
        return self.execute('INSERT OR IGNORE INTO order_categories VALUES(?,?)', (id, category)).rowcount

    def stored_document_extraction(self, digest):
        return self.execute('SELECT extraction FROM documents WHERE sha256=?', (digest,)).fetchone()

    def mark_order_for_review(self, reason, order_id):
        return self.execute(
            "UPDATE source_orders SET pdf_status='review_required',pdf_error=? WHERE id=?",
            (reason, order_id),
        ).rowcount

    def document_hash(self, document_id):
        return self.execute('SELECT sha256 FROM documents WHERE id=?', (document_id,)).fetchone()

    def complete_order_import(self, case_id, document_id, digest, pdf, timestamp, id):
        return self.execute(
            (
                'UPDATE source_orders SET '
                "case_id=?,document_id=?,pdf_sha256=?,pdf_bytes=?,pdf_status='downloaded',pdf_error=NULL,pdf_fetched_at=?"
                ' WHERE id=?'
            ),
            (case_id, document_id, digest, pdf, timestamp, id),
        ).rowcount

    def fail_order_import(self, error, id):
        return self.execute(
            "UPDATE source_orders SET pdf_status='failed',pdf_error=? WHERE id=?",
            (error, id),
        ).rowcount

    def save_source_page(self, filter_key, page, fetched_at, row_count, last_page):
        return self.execute(
            """INSERT INTO source_pages VALUES(?,?,?,?,?) ON CONFLICT(filter_key,page_number)
                        DO UPDATE SET fetched_at=excluded.fetched_at,row_count=excluded.row_count,last_page=excluded.last_page""",
            (filter_key, page, fetched_at, row_count, last_page),
        ).rowcount

    def clear_source_page_failure(self, filter_key, page):
        return self.execute(
            'DELETE FROM source_page_failures WHERE filter_key=? AND page_number=?',
            (filter_key, page),
        ).rowcount

    def advance_source_page(self, next_page, category):
        return self.execute('UPDATE ibbi_sources SET next_page=? WHERE category=?', (next_page, category)).rowcount

    def pending_source_order_count(self, category):
        return self.execute(
            """SELECT COUNT(*) FROM source_orders o JOIN order_categories c ON c.order_id=o.id
                    WHERE c.category=? AND o.pdf_status NOT IN ('skipped_nonfinal','review_required')
                    AND (o.pdf_status<>'downloaded' OR o.case_id IS NULL)""",
            (category,),
        ).fetchone()

    def complete_source_sync(self, started, category):
        return self.execute(
            (
                'UPDATE ibbi_sources SET '
                "last_sync=?,next_page=1,status='completed',error=NULL WHERE category=?"
            ),
            (started, category),
        ).rowcount

    def stop_source_sync(self, status, category):
        return self.execute(
            'UPDATE ibbi_sources SET status=?,next_page=1 WHERE category=?',
            (status, category),
        ).rowcount

    def fail_source_sync(self, error, category):
        return self.execute(
            "UPDATE ibbi_sources SET status='failed',error=? WHERE category=?",
            (error, category),
        ).rowcount

    def record_source_page_failure(self, filter_key, page, error, attempted_at):
        return self.execute(
            """INSERT INTO source_page_failures(filter_key,page_number,last_error,last_attempt_at)
                    VALUES(?,?,?,?) ON CONFLICT(filter_key,page_number) DO UPDATE SET last_error=excluded.last_error,
                    last_attempt_at=excluded.last_attempt_at,attempts=source_page_failures.attempts+1""",
            (filter_key, page, error, attempted_at),
        ).rowcount

    def upsert_listing_filter(self, option_id, name, first_seen_at, last_seen_at):
        return self.execute(
            """INSERT INTO source_filters(key,label,first_seen_at,last_seen_at)
                    VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                    label=excluded.label,last_seen_at=excluded.last_seen_at""",
            (option_id, name, first_seen_at, last_seen_at),
        ).rowcount

    def saved_source_pages(self, key):
        return list(self.execute('SELECT page_number FROM source_pages WHERE filter_key=?', (key,)))

    def upsert_listing_order(
        self,
        id,
        pdf_url,
        order_date,
        subject,
        remarks,
        bench_id,
        value_7,
        stamp,
        stamp_2,
    ):
        return self.execute(
            """INSERT INTO source_orders
                        (id,pdf_url,order_date,subject,remarks,bench_id,bench_name,first_seen_at,last_seen_at)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(pdf_url) DO UPDATE SET
                        order_date=excluded.order_date,subject=excluded.subject,
                        remarks=excluded.remarks,last_seen_at=excluded.last_seen_at,
                        bench_id=COALESCE(excluded.bench_id,source_orders.bench_id),
                        bench_name=COALESCE(excluded.bench_name,source_orders.bench_name)""",
            (id, pdf_url, order_date, subject, remarks, bench_id, value_7, stamp, stamp_2),
        ).rowcount

    def add_source_order_filter(self, order_id, filter_key):
        return self.execute(
            'INSERT OR IGNORE INTO source_order_filters(order_id,filter_key) VALUES(?,?)',
            (order_id, filter_key),
        ).rowcount

    def save_listing_page(self, key, page, stamp, rows, current_last):
        return self.execute(
            """INSERT INTO source_pages(filter_key,page_number,fetched_at,row_count,last_page)
                    VALUES(?,?,?,?,?) ON CONFLICT(filter_key,page_number) DO UPDATE SET
                    fetched_at=excluded.fetched_at,row_count=excluded.row_count,last_page=excluded.last_page""",
            (key, page, stamp, rows, current_last),
        ).rowcount

    def record_listing_page_failure(self, filter_key, page, error, attempted_at):
        return self.execute(
            """INSERT INTO source_page_failures
                            (filter_key,page_number,last_error,last_attempt_at) VALUES(?,?,?,?)
                            ON CONFLICT(filter_key,page_number) DO UPDATE SET
                            last_error=excluded.last_error,last_attempt_at=excluded.last_attempt_at,
                            attempts=source_page_failures.attempts+1""",
            (filter_key, page, error, attempted_at),
        ).rowcount

    def source_order(self, order_id):
        return self.execute('SELECT * FROM source_orders WHERE id=?', (order_id,)).fetchone()

    def complete_pdf_download(self, sha256, byte_count, prefix_bytes, fetched_at, order_id):
        return self.execute(
            """UPDATE source_orders SET pdf_status='downloaded',pdf_error=NULL,
                pdf_sha256=?,pdf_bytes=?,pdf_prefix_bytes=?,pdf_fetched_at=? WHERE id=?""",
            (sha256, byte_count, prefix_bytes, fetched_at, order_id),
        ).rowcount

    def pending_pdf_count(self):
        return self.execute("SELECT COUNT(*) FROM source_orders WHERE pdf_status='pending'", ()).fetchone()

    def global_page_progress(self):
        return self.execute("SELECT COUNT(*),MAX(last_page) FROM source_pages WHERE filter_key='all'", ()).fetchone()

    def listing_import_counts(self):
        return self.execute(
            """SELECT COUNT(*) total,
            COALESCE(SUM(CASE WHEN pdf_status='downloaded' THEN 1 ELSE 0 END),0) downloaded,
            COALESCE(SUM(CASE WHEN pdf_status='failed' THEN 1 ELSE 0 END),0) failed,
            COALESCE(SUM(CASE WHEN case_id IS NOT NULL THEN 1 ELSE 0 END),0) reviewed FROM source_orders""",
            (),
        ).fetchone()

    def global_source_page_counts(self):
        return self.execute(
            "SELECT COUNT(*) done,MAX(last_page) expected FROM source_pages WHERE filter_key='all'",
            (),
        ).fetchone()

    def global_source_page_failure_count(self):
        return self.execute("SELECT COUNT(*) FROM source_page_failures WHERE filter_key='all'", ()).fetchone()

    def synced_bench_count(self):
        return self.execute(
            "SELECT COUNT(DISTINCT filter_key) FROM source_pages WHERE filter_key LIKE 'bench:%%'",
            (),
        ).fetchone()

    def insert_document_fingerprint(self, doc_id, text_hash, name, subject, day):
        return self.execute(
            'INSERT OR IGNORE INTO document_fingerprints VALUES(?,?,?,?,?)',
            (doc_id, text_hash, name, subject, day),
        ).rowcount

    def insert_document_identifier(self, doc_id, kind, value):
        return self.execute(
            'INSERT OR IGNORE INTO document_identifiers VALUES(?,?,?)',
            (doc_id, kind, value),
        ).rowcount

    def matching_fingerprints(self, column, value, doc_id):
        if column not in {'text_hash', 'normalized_name', 'normalized_subject'}:
            raise ValueError('Unknown fingerprint field')
        return list(
            self.execute(
                f'SELECT document_id,ingest_day FROM document_fingerprints WHERE {column}=? AND document_id<>? LIMIT 100',
                (value, doc_id),
            ),
        )

    def matching_identifiers(self, kind, value, doc_id):
        return list(
            self.execute(
                (
                    'SELECT document_id FROM document_identifiers WHERE kind=? AND value=? AND '
                    'document_id<>? LIMIT 100'
                ),
                (kind, value, doc_id),
            ),
        )

    def insert_document_match(self, doc_id, matched, record_json):
        return self.execute(
            'INSERT OR IGNORE INTO document_matches VALUES(?,?,?)',
            (doc_id, matched, record_json),
        ).rowcount

    def stored_document_matches(self, doc_id):
        return list(
            self.execute(
                """
        SELECT m.matched_document_id AS document_id,d.filename,d.source_url,m.reasons
        FROM document_matches m JOIN documents d ON d.id=m.matched_document_id WHERE m.document_id=?""",
                (doc_id,),
            ),
        )

    def document_official_order(self, doc_id):
        return self.execute(
            """SELECT o.order_date,o.subject,d.filename FROM source_orders o
        JOIN documents d ON d.id=o.document_id WHERE o.document_id=? AND o.order_date IS NOT NULL
        ORDER BY o.order_date DESC LIMIT 1""",
            (doc_id,),
        ).fetchone()

    def related_official_documents(self, doc_id, order_date, subject):
        return list(
            self.execute(
                """SELECT DISTINCT o.document_id,d.filename,d.source_url,o.order_date,o.subject
            FROM source_orders o JOIN documents d ON d.id=o.document_id
            WHERE o.document_id<>? AND (o.order_date=? OR LOWER(o.subject)=LOWER(?))""",
                (doc_id, order_date, subject),
            ),
        )

    def case_by_id(self, case_id):
        return self.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()

    def insert_audit(self, case_id, action, record_json, actor, timestamp):
        return self.execute(
            'INSERT INTO audit(case_id,action,snapshot,actor,created_at) VALUES(?,?,?,?,?)',
            (case_id, action, record_json, actor, timestamp),
        ).rowcount

    def case_document_extractions(self, case_id):
        return list(self.execute('SELECT id,extraction FROM documents WHERE case_id=?', (case_id,)))

    def all_cases(self):
        return list(self.execute('SELECT * FROM cases ORDER BY created_at DESC', ()))

    def case_document_summaries(self):
        return list(self.execute('SELECT id,case_id,filename FROM documents ORDER BY created_at,id', ()))

    def case_subjects(self):
        return list(
            self.execute(
                (
                    'SELECT d.case_id,COALESCE(o.subject,d.subject) subject FROM documents d LEFT'
                    ' JOIN source_orders o ON o.document_id=d.id ORDER BY d.created_at,d.id'
                ),
                (),
            ),
        )

    def case_documents(self, case_id):
        return list(
            self.execute(
                (
                    'SELECT '
                    'id,case_id,filename,sha256,source_url,extraction,created_at,subject,order_kind'
                    ' FROM documents WHERE case_id=? ORDER BY created_at'
                ),
                (case_id,),
            ),
        )

    def document_official_subject(self, id):
        return self.execute(
            'SELECT subject FROM source_orders WHERE document_id=? ORDER BY order_date DESC LIMIT 1',
            (id,),
        ).fetchone()

    def case_audit(self, case_id):
        return list(self.execute('SELECT * FROM audit WHERE case_id=? ORDER BY id DESC', (case_id,)))

    def save_case_draft(self, record_json, case_id, version):
        return self.execute(
            (
                'UPDATE cases SET '
                "record=?,version=version+1,review_status='draft',reviewed_by=NULL,reviewed_at=NULL"
                ' WHERE id=? AND version=?'
            ),
            (record_json, case_id, version),
        ).rowcount

    def approve_case(self, reviewer, timestamp, case_id, version):
        return self.execute(
            (
                'UPDATE cases SET '
                "review_status='approved',version=version+1,reviewed_by=?,reviewed_at=? WHERE"
                ' id=? AND version=?'
            ),
            (reviewer, timestamp, case_id, version),
        ).rowcount

    def document_by_hash(self, digest):
        return self.execute('SELECT id,case_id FROM documents WHERE sha256=?', (digest,)).fetchone()

    def insert_case(self, case_id, record_json, timestamp):
        return self.execute(
            'INSERT INTO cases(id,record,created_at) VALUES(?,?,?)',
            (case_id, record_json, timestamp),
        ).rowcount

    def replace_case_record(self, record_json, case_id):
        return self.execute('UPDATE cases SET record=? WHERE id=?', (record_json, case_id)).rowcount

    def mark_case_draft(self, case_id):
        return self.execute(
            (
                'UPDATE cases SET '
                "review_status='draft',version=version+1,reviewed_by=NULL,reviewed_at=NULL "
                'WHERE id=?'
            ),
            (case_id,),
        ).rowcount

    def insert_document(
        self,
        doc_id,
        case_id,
        filename,
        digest,
        source_url,
        record_json,
        timestamp,
        content,
        subject,
    ):
        return self.execute(
            """INSERT INTO documents
                    (id,case_id,filename,sha256,source_url,extraction,created_at,content,subject)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                doc_id,
                case_id,
                filename,
                digest,
                source_url,
                record_json,
                timestamp,
                content,
                subject,
            ),
        ).rowcount

    def document_source(self, doc_id):
        return self.execute(
            'SELECT filename,sha256,extraction,content FROM documents WHERE id=?',
            (doc_id,),
        ).fetchone()

    def cached_source_evidence(self, doc_id):
        return self.execute('SELECT * FROM document_source_evidence WHERE document_id=?', (doc_id,)).fetchone()

    def cache_source_evidence(self, doc_id, sha256, SOURCE_VERSION, record_json):
        return self.execute(
            """INSERT INTO document_source_evidence(document_id,sha256,reader_version,content)
                    VALUES(?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET
                    sha256=excluded.sha256,reader_version=excluded.reader_version,content=excluded.content""",
            (doc_id, sha256, SOURCE_VERSION, record_json),
        ).rowcount

    def stored_pdf_bytes(self, document_id):
        return self.execute('SELECT content FROM documents WHERE id=?', (document_id,)).fetchone()

    def document_for_extraction(self, doc_id):
        return self.execute(
            'SELECT case_id,content,extraction,filename,created_at FROM documents WHERE id=?',
            (doc_id,),
        ).fetchone()

    def case_audit_snapshots(self, id):
        return self.execute('SELECT action,snapshot FROM audit WHERE case_id=?', (id,)).fetchall()

    def case_document_count(self, id):
        return self.execute('SELECT COUNT(*) FROM documents WHERE case_id=?', (id,)).fetchone()

    def update_document_extraction(self, record_json, doc_id):
        return self.execute('UPDATE documents SET extraction=? WHERE id=?', (record_json, doc_id)).rowcount

    def update_document_subject(self, subject, doc_id):
        return self.execute('UPDATE documents SET subject=? WHERE id=?', (subject, doc_id)).rowcount

    def delete_document_matches(self, doc_id, doc_id_2):
        return self.execute(
            'DELETE FROM document_matches WHERE document_id=? OR matched_document_id=?',
            (doc_id, doc_id_2),
        ).rowcount

    def delete_document_identifiers(self, doc_id):
        return self.execute('DELETE FROM document_identifiers WHERE document_id=?', (doc_id,)).rowcount

    def delete_document_fingerprints(self, doc_id):
        return self.execute('DELETE FROM document_fingerprints WHERE document_id=?', (doc_id,)).rowcount

    def refresh_case_draft(self, record_json, case_id, version, review_status):
        return self.execute(
            'UPDATE cases SET record=?,version=version+1 WHERE id=? AND version=? AND review_status=?',
            (record_json, case_id, version, review_status),
        ).rowcount

    def import_job_exists(self, job_id):
        return self.execute('SELECT id FROM import_jobs WHERE id=?', (job_id,)).fetchone()

    def update_upload_job(self, job_label, status, record_json, stamp, job_id):
        return self.execute(
            'UPDATE import_jobs SET url=?,status=?,result=?,updated_at=? WHERE id=?',
            (job_label, status, record_json, stamp, job_id),
        ).rowcount

    def document_metadata(self, document_id):
        return self.execute(
            """SELECT filename,subject,source_url,created_at,order_kind,sha256
                        FROM documents WHERE id=?""",
            (document_id,),
        ).fetchone()

    def stored_pdf(self, document_id):
        return self.execute('SELECT id,content,filename FROM documents WHERE id=?', (document_id,)).fetchone()

    def recent_import_jobs(self):
        return list(self.execute('SELECT * FROM import_jobs ORDER BY created_at DESC LIMIT 20', ()))

    def import_job(self, job_id):
        return self.execute('SELECT * FROM import_jobs WHERE id=?', (job_id,)).fetchone()

    def import_job_count(self):
        return self.execute('SELECT COUNT(*) FROM import_jobs', ()).fetchone()

    def import_job_page(self, page_size, offset):
        return self.execute(
            'SELECT * FROM import_jobs ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
            (page_size, offset),
        ).fetchall()

    def import_job_subjects(self, id):
        return list(
            self.execute(
                (
                    'SELECT DISTINCT COALESCE(o.subject,d.subject) subject FROM import_files f '
                    'JOIN documents d ON d.id=f.document_id LEFT JOIN source_orders o ON '
                    'o.pdf_url=f.source_url WHERE f.job_id=? AND COALESCE(o.subject,d.subject) IS'
                    ' NOT NULL ORDER BY subject LIMIT 3'
                ),
                (id,),
            ),
        )

    def source_order_counts(self):
        return self.execute(
            """SELECT COUNT(*) total,
                COALESCE(SUM(CASE WHEN pdf_status='downloaded' THEN 1 ELSE 0 END),0) downloaded,
                COALESCE(SUM(CASE WHEN pdf_status='failed' THEN 1 ELSE 0 END),0) failed,
                COALESCE(SUM(CASE WHEN case_id IS NOT NULL THEN 1 ELSE 0 END),0) in_review
                FROM source_orders""",
            (),
        ).fetchone()

    def source_page_counts(self, category_pattern):
        return self.execute(
            (
                'SELECT COUNT(*) done,MAX(last_page) expected FROM source_pages WHERE '
                "filter_key='all' OR filter_key LIKE ?"
            ),
            (category_pattern,),
        ).fetchone()

    def source_page_failure_count(self, category_pattern):
        return self.execute(
            "SELECT COUNT(*) FROM source_page_failures WHERE filter_key='all' OR filter_key LIKE ?",
            (category_pattern,),
        ).fetchone()

    def source_filters(self):
        return list(self.execute('SELECT key,label FROM source_filters ORDER BY label', ()))

    def source_remarks(self):
        return list(
            self.execute(
                "SELECT DISTINCT remarks FROM source_orders WHERE remarks!='' ORDER BY remarks",
                (),
            ),
        )

    def source_categories(self):
        return list(self.execute('SELECT category FROM ibbi_sources ORDER BY category', ()))

    def source_orders_by_year(self):
        return list(
            self.execute(
                """SELECT substr(order_date,1,4) AS "year",COUNT(*) AS orders
                FROM source_orders WHERE order_date IS NOT NULL
                GROUP BY substr(order_date,1,4) ORDER BY substr(order_date,1,4)""",
                (),
            ),
        )

    def source_orders_by_remark(self):
        return list(
            self.execute(
                """SELECT remarks,COUNT(*) orders FROM source_orders
                GROUP BY remarks ORDER BY orders DESC LIMIT 15""",
                (),
            ),
        )

    def source_order_date_range(self):
        return self.execute(
            'SELECT MIN(order_date) earliest,MAX(order_date) latest FROM source_orders',
            (),
        ).fetchone()

    def link_source_document(self, case_id, document_id, order_id):
        return self.execute(
            'UPDATE source_orders SET case_id=?,document_id=? WHERE id=?',
            (case_id, document_id, order_id),
        ).rowcount

    def interrupt_orphaned_jobs(self, timestamp):
        self.execute(
            (
                "UPDATE import_jobs SET status='interrupted' WHERE status IN "
                "('queued','running') AND id NOT IN (SELECT active_job FROM "
                'scheduler_settings WHERE active_job IS NOT NULL AND lease_until>?)'
            ),
            (timestamp,),
        )

    @staticmethod
    def setup_postgres():
        url = os.environ.get('DATABASE_URL', '')
        parsed = urlparse(url)
        name = parsed.path.lstrip('/')
        if parsed.scheme not in {'postgresql', 'postgres'} or not name or '/' in name:
            raise SystemExit('Set DATABASE_URL in .env to a PostgreSQL database URL first')
        admin_url = parsed._replace(path='/postgres').geturl()
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=10) as db:
            exists = db.execute('SELECT 1 FROM pg_database WHERE datname=%s', (name,)).fetchone()
            if not exists:
                db.execute(
                    sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(sql.Identifier(name)),
                )
                print(f'Created database {name}')
            else:
                print(f'Database {name} already exists')
        initialize(ROOT / 'data', database_url=url)
        print('PostgreSQL tables ready')

    @staticmethod
    def reset_postgres(reset):
        url = os.environ.get('DATABASE_URL', '')
        parsed = urlsplit(url)
        if parsed.hostname not in {'localhost', '127.0.0.1', '::1'} or parsed.path != '/ibc':
            raise SystemExit('Reset is restricted to the local ibc project database.')
        required = set(
            re.findall(
                'CREATE TABLE IF NOT EXISTS\\s+(\\w+)',
                '\n'.join(
                    (
                        Database.SQLITE_SCHEMA,
                        Database.STRUCTURED_SCHEMA,
                        Database.AUTOMATIC_SCHEMA,
                        Database.MATCHING_SCHEMA,
                    ),
                ),
            ),
        )
        with psycopg.connect(url, connect_timeout=10) as db:
            tables = {r[0] for r in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
            extras = tables - required
            if extras:
                raise SystemExit(
                    'Unrecognized tables require review before resetting: ' + ', '.join(sorted(extras)),
                )
            names = sorted(tables & required)
            counts = {name: db.execute(sql.SQL('SELECT COUNT(*) FROM {}').format(sql.Identifier('public', name))).fetchone()[0] for name in names}
            print('Database: ibc; required tables:', len(names))
            print('Existing rows:', sum(counts.values()))
            if not reset:
                print('Inspection only. Pass --reset to delete the project records.')
                return
            db.execute("SET LOCAL lock_timeout='10s'")
            db.execute(
                sql.SQL('TRUNCATE TABLE {} RESTART IDENTITY').format(
                    sql.SQL(',').join((sql.Identifier('public', name) for name in names)),
                ),
            )
        initialize(ROOT / 'data', database_url=url)
        with psycopg.connect(url, connect_timeout=10) as db:
            data_tables = required - {'scheduler_settings', 'ibbi_sources'}
            assert all(
                (db.execute(
                    sql.SQL('SELECT COUNT(*) FROM {}').format(sql.Identifier('public', name)),
                ).fetchone()[0] == 0 for name in data_tables),
            )
            assert db.execute('SELECT enabled FROM scheduler_settings WHERE id=1').fetchone()[0] == 0
        print(
            'Reset complete: all project records deleted; required tables retained; scheduler disabled.',
        )
        print('Only fresh scheduler settings and default IBBI source definitions remain.')

    @staticmethod
    def migrate_one(document_id: str, root: Path=ROOT / 'data'):
        url = os.environ.get('DATABASE_URL', '')
        if not url:
            raise ValueError('DATABASE_URL is not set in .env')
        source_path = root / 'ibc.sqlite3'
        if not source_path.exists():
            raise ValueError('Source SQLite database is missing')
        source = sqlite3.connect(source_path)
        source.row_factory = sqlite3.Row
        try:
            document = source.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if not document:
                raise ValueError('Selected document is not in the SQLite database')
            case_id = document['case_id']
            count = source.execute('SELECT COUNT(*) FROM documents WHERE case_id=?', (case_id,)).fetchone()[0]
            if count != 1:
                raise ValueError(
                    'This case has multiple documents; one-file migration would leave broken citations',
                )
            case = source.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
            history = source.execute('SELECT * FROM audit WHERE case_id=? ORDER BY id', (case_id,)).fetchall()
            content = (root / 'documents' / f'{document_id}.pdf').read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if digest != document['sha256']:
                raise ValueError('Selected PDF does not match the database SHA-256')
            record = json.loads(case['record'])

            def citations(value):
                if isinstance(value, dict):
                    if 'document_id' in value:
                        yield value['document_id']
                    for child in value.values():
                        yield from citations(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from citations(child)
            if any((ref != document_id for ref in citations(record))):
                raise ValueError('Case record refers to a different document')
            initialize(root, database_url=url)
            with connection(root, database_url=url) as target:
                existing = target.execute('SELECT id FROM cases WHERE id=?', (case_id,)).fetchone()
                if not existing:
                    target.execute(
                        """INSERT INTO cases
                    (id,record,version,review_status,created_at,reviewed_by,reviewed_at)
                    VALUES(?,?,?,?,?,?,?)""",
                        (
                            case['id'],
                            case['record'],
                            case['version'],
                            case['review_status'],
                            case['created_at'],
                            case['reviewed_by'],
                            case['reviewed_at'],
                        ),
                    )
                    for item in history:
                        target.execute(
                            """INSERT INTO audit(case_id,action,snapshot,actor,created_at)
                        VALUES(?,?,?,?,?)""",
                            (
                                item['case_id'],
                                item['action'],
                                item['snapshot'],
                                item['actor'],
                                item['created_at'],
                            ),
                        )
                existing_doc = target.execute('SELECT sha256,content FROM documents WHERE id=?', (document_id,)).fetchone()
                if not existing_doc:
                    target.execute(
                        """INSERT INTO documents
                    (id,case_id,filename,sha256,source_url,extraction,created_at,content)
                    VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            document['id'],
                            case_id,
                            document['filename'],
                            document['sha256'],
                            document['source_url'],
                            document['extraction'],
                            document['created_at'],
                            content,
                        ),
                    )
                elif existing_doc['sha256'] != digest or bytes(existing_doc['content'] or b'') != content:
                    raise ValueError('PostgreSQL already has a different copy of this document')
            with connection(root, database_url=url) as target:
                stored = target.execute('SELECT sha256,content FROM documents WHERE id=?', (document_id,)).fetchone()
                if hashlib.sha256(bytes(stored['content'])).hexdigest() != digest:
                    raise ValueError('PostgreSQL PDF verification failed')
            return {
                'case_id': case_id,
                'document_id': document_id,
                'sha256': digest,
                'bytes': len(content),
                'audit_events': len(history),
            }
        finally:
            source.close()
    TABLES = (
        'audit',
        'case_details',
        'claims',
        'document_categories',
        'document_fingerprints',
        'document_identifiers',
        'document_matches',
        'document_source_evidence',
        'documents',
        'entities',
        'import_files',
        'import_jobs',
        'order_categories',
        'people',
        'plans',
        'source_filters',
        'source_order_filters',
        'source_orders',
        'source_page_failures',
        'source_pages',
    )

    @staticmethod
    def sqlite_tables(db):
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def reset_project_records(self):
        if self.root.resolve() != (ROOT / 'data').resolve():
            raise SystemExit('Refusing to reset an unexpected IBC_DATA_DIR')
        url = os.environ.get('DATABASE_URL', '')
        if url:
            parsed = urlparse(url)
            if parsed.path != '/ibc' or parsed.hostname not in {'localhost', '127.0.0.1'}:
                raise SystemExit('Refusing to reset a PostgreSQL database other than local /ibc')
            with connection(self.root.resolve()) as db:
                names = {row[0] for row in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
                print(
                    'PostgreSQL before:',
                    {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in ('cases', 'documents', 'source_orders', 'import_jobs') if name in names},
                )
                if 'scheduler_settings' in names:
                    db.execute(
                        'UPDATE scheduler_settings SET enabled=0,next_run=NULL,lease_until=NULL,active_job=NULL',
                    )
                targets = sorted(names.intersection(set(self.TABLES) | {'cases'}))
                db.execute('TRUNCATE TABLE ' + ', '.join(targets) + ' RESTART IDENTITY CASCADE')
                if 'ibbi_sources' in names:
                    db.execute(
                        (
                            'UPDATE ibbi_sources SET '
                            "last_sync=NULL,last_attempt=NULL,next_page=1,status='pending',error=NULL"
                        ),
                    )
                if 'scheduler_settings' in names:
                    db.execute(
                        (
                            'UPDATE scheduler_settings SET '
                            "enabled=0,category='all',next_run=NULL,lease_until=NULL,active_job=NULL"
                        ),
                    )
        sqlite_path = self.root.resolve() / 'ibc.sqlite3'
        if sqlite_path.exists():
            with sqlite3.connect(sqlite_path) as db:
                names = self.sqlite_tables(db)
                print(
                    'SQLite before:',
                    {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in ('cases', 'documents', 'source_orders', 'import_jobs') if name in names},
                )
                db.execute('PRAGMA foreign_keys=OFF')
                for name in sorted(names.intersection(set(self.TABLES) | {'cases'})):
                    db.execute(f'DELETE FROM {name}')
                if 'ibbi_sources' in names:
                    db.execute(
                        (
                            'UPDATE ibbi_sources SET '
                            "last_sync=NULL,last_attempt=NULL,next_page=1,status='pending',error=NULL"
                        ),
                    )
                if 'scheduler_settings' in names:
                    db.execute(
                        (
                            'UPDATE scheduler_settings SET '
                            "enabled=0,category='all',next_run=NULL,lease_until=NULL,active_job=NULL"
                        ),
                    )
            with sqlite3.connect(sqlite_path) as db:
                db.execute('VACUUM')

    def verify_project_reset(self):
        url = os.environ.get('DATABASE_URL', '')
        sqlite_path = self.root / 'ibc.sqlite3'
        with connection(self.root.resolve(), database_url=url) as db:
            print(
                'Active database after:',
                {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in ('cases', 'documents', 'source_orders', 'import_jobs')},
            )
            if any(
                (db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in ('cases', 'documents', 'source_orders', 'import_jobs')),
            ):
                raise RuntimeError('Active database still has project records')
        if sqlite_path.exists():
            with sqlite3.connect(sqlite_path) as db:
                names = self.sqlite_tables(db)
                remaining = {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in sorted(names.intersection(set(self.TABLES) | {'cases'}))}
                print(
                    'SQLite after:',
                    {name: remaining[name] for name in ('cases', 'documents', 'source_orders', 'import_jobs') if name in remaining},
                )
                if any(remaining.values()):
                    raise RuntimeError(f'SQLite still has project records: {remaining}')

    def stored_order_document(self, pdf_url):
        return self.execute(
            (
                'SELECT d.id,d.sha256,d.content,o.id order_id FROM source_orders o JOIN '
                'documents d ON d.id=o.document_id WHERE o.pdf_url=?'
            ),
            (pdf_url,),
        ).fetchone()

    def import_document_links(self, pdf_url):
        return self.execute('SELECT DISTINCT document_id FROM import_files WHERE source_url=?', (pdf_url,)).fetchall()

    def repair_order_metadata(self, subject, order_date, remarks, digest, pdf, order_id):
        return self.execute(
            (
                'UPDATE source_orders SET '
                'subject=?,order_date=?,remarks=?,pdf_sha256=?,pdf_bytes=? WHERE id=?'
            ),
            (subject, order_date, remarks, digest, pdf, order_id),
        ).rowcount
connection = Database.connection
decode = Database.decode
SQLITE_SCHEMA = Database.SQLITE_SCHEMA
POSTGRES_SCHEMA = Database.POSTGRES_SCHEMA

def initialize(root, database_url=None):
    Database(root, database_url).initialize()
