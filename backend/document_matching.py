"""Indexed review candidates; only a byte hash can suppress storage."""
import hashlib
import json
import re
from pathlib import PurePosixPath
from .structured import normalized

SCHEMA = """
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


def fingerprint(db, doc_id, filename, extraction, created_at):
    text = ' '.join(' '.join(p['text'] for p in extraction['pages']).casefold().split())
    text_hash = hashlib.sha256(text.encode()).hexdigest() if len(text)>200 else None
    name, subject = normalized(PurePosixPath(filename).stem), normalized(extraction.get('subject') or '')
    day = created_at[:10]
    db.execute('INSERT OR IGNORE INTO document_fingerprints VALUES(?,?,?,?,?)',(doc_id,text_hash,name,subject,day))
    identifiers = set(re.findall(r'\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b', text.upper()))
    ids = [('CIN',value) for value in identifiers]
    ids += [('DIN',value) for value in re.findall(r'\bDIN\s*[:\-]?\s*(\d{8})\b',text.upper())]
    for kind,value in set(ids):
        db.execute('INSERT OR IGNORE INTO document_identifiers VALUES(?,?,?)',(doc_id,kind,value))
    candidates = {}
    for column,value,reason in [('text_hash',text_hash,'same_extracted_text'),('normalized_name',name,'same_filename'),
                                ('normalized_subject',subject,'same_subject')]:
        if value:
            for row in db.execute(f'SELECT document_id,ingest_day FROM document_fingerprints WHERE {column}=? AND document_id<>? LIMIT 100',(value,doc_id)):
                reasons = candidates.setdefault(row['document_id'],set())
                reasons.add(reason)
                if row['ingest_day']==day and reason=='same_filename':
                    reasons.add('same_day_and_filename')
    for kind,value in set(ids):
        for row in db.execute('SELECT document_id FROM document_identifiers WHERE kind=? AND value=? AND document_id<>? LIMIT 100',(kind,value,doc_id)):
            candidates.setdefault(row['document_id'],set()).add('shared_'+kind)
    for matched,reasons in candidates.items():
        db.execute('INSERT OR IGNORE INTO document_matches VALUES(?,?,?)',(doc_id,matched,json.dumps(sorted(reasons))))
        db.execute('INSERT OR IGNORE INTO document_matches VALUES(?,?,?)',(matched,doc_id,json.dumps(sorted(reasons))))


def initialize_matching(db):
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    rows = db.execute('SELECT id,filename,extraction,created_at FROM documents WHERE id NOT IN (SELECT document_id FROM document_fingerprints)').fetchall()
    for row in rows:
        fingerprint(db,row['id'],row['filename'],json.loads(row['extraction']),row['created_at'])


def candidates(db, doc_id):
    matches = {row['document_id']: {**dict(row),'reasons':json.loads(row['reasons'])} for row in db.execute('''
        SELECT m.matched_document_id AS document_id,d.filename,d.source_url,m.reasons
        FROM document_matches m JOIN documents d ON d.id=m.matched_document_id WHERE m.document_id=?''',(doc_id,))}
    order = db.execute('''SELECT o.order_date,o.subject,d.filename FROM source_orders o
        JOIN documents d ON d.id=o.document_id WHERE o.document_id=? AND o.order_date IS NOT NULL
        ORDER BY o.order_date DESC LIMIT 1''',(doc_id,)).fetchone()
    if order:
        name = normalized(order['subject'] or '')
        for row in db.execute('''SELECT DISTINCT o.document_id,d.filename,d.source_url,o.order_date,o.subject
            FROM source_orders o JOIN documents d ON d.id=o.document_id
            WHERE o.document_id<>? AND (o.order_date=? OR LOWER(o.subject)=LOWER(?))''',
            (doc_id,order['order_date'],order['subject'])):
            reasons = []
            if len(name) >= 12 and normalized(row['subject'] or '') == name:
                reasons.append('same_official_order_name')
                if row['order_date'] == order['order_date']:
                    reasons.append('same_order_date_and_name')
            if row['order_date'] == order['order_date'] and normalized(PurePosixPath(row['filename']).stem) == normalized(PurePosixPath(order['filename']).stem):
                reasons.append('same_order_date_and_filename')
            if not reasons:
                continue  # A shared order date alone is not evidence of a duplicate.
            match = matches.setdefault(row['document_id'], {'document_id':row['document_id'],
                'filename':row['filename'],'source_url':row['source_url'],'reasons':[]})
            match['reasons'] = sorted(set(match['reasons']) | set(reasons))
    return list(matches.values())[:20]
