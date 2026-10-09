"""Explicitly reset the local ibc database while keeping the application's required schema."""
import argparse
import os
import re
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

from backend.db import ROOT, SQLITE_SCHEMA, initialize
from backend.structured import SCHEMA as STRUCTURED
from backend.automatic_orders import SCHEMA as AUTOMATIC
from backend.document_matching import SCHEMA as MATCHING


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reset',action='store_true',help='Delete all project records; keep required tables')
    args=parser.parse_args()
    url=os.environ.get('DATABASE_URL','')
    parsed=urlsplit(url)
    if parsed.hostname not in {'localhost','127.0.0.1','::1'} or parsed.path!='/ibc':
        raise SystemExit('Reset is restricted to the local ibc project database.')
    required=set(re.findall(r'CREATE TABLE IF NOT EXISTS\s+(\w+)', '\n'.join((SQLITE_SCHEMA,STRUCTURED,AUTOMATIC,MATCHING))))
    with psycopg.connect(url,connect_timeout=10) as db:
        tables={r[0] for r in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        extras=tables-required
        if extras:
            raise SystemExit('Unrecognized tables require review before resetting: '+', '.join(sorted(extras)))
        names=sorted(tables & required)
        counts={name:db.execute(sql.SQL('SELECT COUNT(*) FROM {}').format(sql.Identifier('public',name))).fetchone()[0] for name in names}
        print('Database: ibc; required tables:',len(names))
        print('Existing rows:',sum(counts.values()))
        if not args.reset:
            print('Inspection only. Pass --reset to delete the project records.')
            return
        db.execute("SET LOCAL lock_timeout='10s'")
        # No CASCADE: unexpected outside dependencies must fail instead of being erased.
        db.execute(sql.SQL('TRUNCATE TABLE {} RESTART IDENTITY').format(
            sql.SQL(',').join(sql.Identifier('public',name) for name in names)))
    initialize(ROOT/'data',database_url=url)
    with psycopg.connect(url,connect_timeout=10) as db:
        data_tables=required-{'scheduler_settings','ibbi_sources'}
        assert all(db.execute(sql.SQL('SELECT COUNT(*) FROM {}').format(sql.Identifier('public',name))).fetchone()[0]==0 for name in data_tables)
        assert db.execute('SELECT enabled FROM scheduler_settings WHERE id=1').fetchone()[0]==0
    print('Reset complete: all project records deleted; required tables retained; scheduler disabled.')
    print('Only fresh scheduler settings and default IBBI source definitions remain.')


if __name__=='__main__':
    main()
