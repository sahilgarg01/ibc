"""Remove this project's stored records and downloaded PDFs, retaining schema and samples."""
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from backend.db import ROOT, connection


DATA = Path(os.environ.get('IBC_DATA_DIR', ROOT / 'data')).resolve()
TABLES = (
    'audit', 'case_details', 'claims', 'document_categories', 'document_fingerprints',
    'document_identifiers', 'document_matches', 'documents', 'entities', 'import_files',
    'import_jobs', 'order_categories', 'people', 'plans', 'source_filters',
    'source_order_filters', 'source_orders', 'source_page_failures', 'source_pages',
)


def sqlite_tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def main():
    if DATA != (ROOT / 'data').resolve():
        raise SystemExit('Refusing to reset an unexpected IBC_DATA_DIR')
    url = os.environ.get('DATABASE_URL', '')
    if url:
        parsed = urlparse(url)
        if parsed.path != '/ibc' or parsed.hostname not in {'localhost', '127.0.0.1'}:
            raise SystemExit('Refusing to reset a PostgreSQL database other than local /ibc')
        with connection(DATA) as db:
            names = {row[0] for row in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
            print('PostgreSQL before:', {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                                         for name in ('cases', 'documents', 'source_orders', 'import_jobs') if name in names})
            if 'scheduler_settings' in names:
                db.execute("UPDATE scheduler_settings SET enabled=0,next_run=NULL,lease_until=NULL,active_job=NULL")
            targets = sorted(names.intersection(set(TABLES) | {'cases'}))
            db.execute('TRUNCATE TABLE ' + ', '.join(targets) + ' RESTART IDENTITY CASCADE')
            if 'ibbi_sources' in names:
                db.execute("UPDATE ibbi_sources SET last_sync=NULL,last_attempt=NULL,next_page=1,status='pending',error=NULL")
            if 'scheduler_settings' in names:
                db.execute("UPDATE scheduler_settings SET enabled=0,category='all',next_run=NULL,lease_until=NULL,active_job=NULL")
    sqlite_path = DATA / 'ibc.sqlite3'
    if sqlite_path.exists():
        with sqlite3.connect(sqlite_path) as db:
            names = sqlite_tables(db)
            print('SQLite before:', {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                                     for name in ('cases', 'documents', 'source_orders', 'import_jobs') if name in names})
            db.execute('PRAGMA foreign_keys=OFF')
            for name in sorted(names.intersection(set(TABLES) | {'cases'})):
                db.execute(f'DELETE FROM {name}')
            if 'ibbi_sources' in names:
                db.execute("UPDATE ibbi_sources SET last_sync=NULL,last_attempt=NULL,next_page=1,status='pending',error=NULL")
            if 'scheduler_settings' in names:
                db.execute("UPDATE scheduler_settings SET enabled=0,category='all',next_run=NULL,lease_until=NULL,active_job=NULL")
        with sqlite3.connect(sqlite_path) as db:
            db.execute('VACUUM')
    for folder in (DATA / 'documents', DATA / 'source_documents'):
        folder = folder.resolve()
        if folder.parent != DATA:
            raise SystemExit('Refusing to remove PDFs outside the project data directory')
        pdfs = list(folder.glob('*.pdf')) if folder.exists() else []
        for pdf in pdfs:
            pdf.unlink()
        print(f'Removed {len(pdfs)} PDFs from {folder.name}')
    with connection(DATA, database_url=url) as db:
        print('Active database after:', {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                                         for name in ('cases', 'documents', 'source_orders', 'import_jobs')})
        if any(db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in ('cases', 'documents', 'source_orders', 'import_jobs')):
            raise RuntimeError('Active database still has project records')
    if sqlite_path.exists():
        with sqlite3.connect(sqlite_path) as db:
            names = sqlite_tables(db)
            remaining = {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                         for name in sorted(names.intersection(set(TABLES) | {'cases'}))}
            print('SQLite after:', {name: remaining[name] for name in ('cases', 'documents', 'source_orders', 'import_jobs') if name in remaining})
            if any(remaining.values()):
                raise RuntimeError(f'SQLite still has project records: {remaining}')


if __name__ == '__main__':
    main()
