"""Reprocess a stored scanned PDF and verify that its API content contains text."""
import argparse
import os
import psycopg
from fastapi.testclient import TestClient
from backend.main import app


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('filename')
    args=parser.parse_args()
    with psycopg.connect(os.environ['DATABASE_URL']) as db:
        row=db.execute('SELECT id FROM documents WHERE filename=%s',(args.filename,)).fetchone()
    if not row: raise SystemExit('Document not found')
    client=TestClient(app)  # Use existing tables without starting another scheduler.
    response=client.post('/api/documents/'+row[0]+'/extract-facts')
    response.raise_for_status()
    content=client.get('/api/documents/'+row[0]+'/content').json()
    counts=[len(p['text'].strip()) for p in content['pages']]
    print('Page text lengths:',counts)
    print('Methods:',[p.get('extraction_method') for p in content['pages']])
    assert all(count>30 for count in counts),content['warnings']
    print('Stored PDF content repaired and verified through the API.')


if __name__=='__main__': main()
