"""Transactional projections of evidence records and conservative entity matching."""
import json
import re
from difflib import SequenceMatcher
from uuid import uuid4

from .models import CaseRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 kind TEXT NOT NULL, identifier TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS entity_name_idx ON entities(normalized_name);
CREATE TABLE IF NOT EXISTS case_details (
 case_id TEXT PRIMARY KEY REFERENCES cases(id), company TEXT, cin TEXT,
 company_entity_id TEXT REFERENCES entities(id), nclt_bench TEXT,
 admission_date TEXT, resolution_date TEXT, irp TEXT, rp TEXT, outcome TEXT NOT NULL
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


def normalized(name):
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def entity(db, name, kind, identifier=None):
    # Names alone are not proof of identity. Identifier-less mentions stay distinct.
    if identifier:
        row = db.execute("SELECT id FROM entities WHERE identifier=?", (identifier,)).fetchone()
        if row:
            return row["id"]
    key = uuid4().hex
    db.execute("INSERT INTO entities(id,name,normalized_name,kind,identifier) VALUES(?,?,?,?,?)",
               (key, name, normalized(name), kind, identifier))
    return key


def project(db, case_id, record):
    r = CaseRecord.model_validate(record)
    value = lambda f: f.value if f else None
    for table in ("case_details", "claims", "people", "plans"):
        db.execute(f"DELETE FROM {table} WHERE case_id=?", (case_id,))
    company_id = entity(db, r.company.value, "company", "CIN:" + r.cin.value.upper() if r.cin else None) if r.company else None
    db.execute("INSERT INTO case_details VALUES(?,?,?,?,?,?,?,?,?,?)",
               (case_id, value(r.company), value(r.cin), company_id, value(r.tribunal),
                value(r.admission_date), value(r.resolution_date), value(r.irp), value(r.professional), r.outcome))
    for c in r.claims:
        db.execute("INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uuid4().hex, case_id,
                   c.creditor, c.category, c.scope, str(c.claimed) if c.claimed is not None else None,
                   str(c.admitted) if c.admitted is not None else None,
                   str(c.plan_amount) if c.plan_amount is not None else None,
                   str(c.actual_paid) if c.actual_paid is not None else None, c.document_id, c.page))
    for field, role, period in (("previous_owners", "owner", "before"), ("current_owners", "owner", "current"),
                                ("subsequent_owners", "owner", "after"), ("previous_directors", "director", "before"),
                                ("current_directors", "director", "current"), ("subsequent_directors", "director", "after")):
        for f in getattr(r, field):
            identifier = "DIN:" + f.din if f.din else "CIN:" + f.cin if f.cin else None
            eid = entity(db, f.value, "company" if f.cin else "person", identifier)
            db.execute("INSERT INTO people VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid4().hex, case_id, eid,
                       f.value, role, period, f.din, f.cin, f.document_id, f.page))
    for p in r.plans:
        haircut = (p.admitted-p.amount)/p.admitted*100 if p.admitted and p.amount is not None else None
        db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)", (uuid4().hex, case_id, p.applicant,
                   str(p.amount) if p.amount is not None else None, str(p.admitted) if p.admitted is not None else None,
                   str(round(haircut, 2)) if haircut is not None else None, p.document_id, p.page))
    # Delete obsolete mentions after rebuilding; identified entities remain available for reuse.
    db.execute("""DELETE FROM entities WHERE identifier IS NULL
        AND id NOT IN (SELECT entity_id FROM people)
        AND id NOT IN (SELECT company_entity_id FROM case_details WHERE company_entity_id IS NOT NULL)""")


def initialize_structured(db):
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    for row in db.execute("SELECT id,record FROM cases WHERE id NOT IN (SELECT case_id FROM case_details)").fetchall():
        project(db, row["id"], json.loads(row["record"]))
    for job in db.execute("SELECT id,created_at,result FROM import_jobs").fetchall():
        persist_import_files(db, job["id"], json.loads(job["result"]), job["created_at"])


def persist_import_files(db, job_id, result, timestamp):
    for item in result.get("items", []):
        doc_id = item.get("document_id")
        if not doc_id:
            docs = db.execute("SELECT id,source_url FROM documents WHERE case_id=?", (item["case_id"],)).fetchall()
            matches = [d for d in docs if d["source_url"] == item["url"]]
            doc_id = matches[0]["id"] if len(matches) == 1 else None
        if doc_id:
            db.execute("INSERT OR IGNORE INTO import_files VALUES(?,?,?,?,?,?)",
                       (uuid4().hex, job_id, doc_id, item["url"], int(item["duplicate"]), timestamp))


def match_candidates(db, name, din=None, cin=None):
    identifier = "DIN:" + din if din else "CIN:" + cin if cin else None
    if identifier:
        return [{**dict(r), "match": "identifier", "score": 1.0} for r in
                db.execute("SELECT * FROM entities WHERE identifier=?", (identifier,))]
    candidates = []
    for row in db.execute("SELECT * FROM entities"):
        score = SequenceMatcher(None, normalized(name), row["normalized_name"]).ratio()
        if score >= .8:
            candidates.append({**dict(row), "match": "name_candidate_requires_review", "score": round(score, 3)})
    return sorted(candidates, key=lambda c: c["score"], reverse=True)[:20]
