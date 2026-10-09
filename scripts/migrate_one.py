"""Copy one reviewed document and its one-document case from SQLite to PostgreSQL."""

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from backend.db import connection, initialize

ROOT = Path(__file__).resolve().parents[1]


def migrate_one(document_id: str, root: Path = ROOT / "data"):
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise ValueError("DATABASE_URL is not set in .env")
    source_path = root / "ibc.sqlite3"
    if not source_path.exists():
        raise ValueError("Source SQLite database is missing")
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    try:
        document = source.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not document:
            raise ValueError("Selected document is not in the SQLite database")
        case_id = document["case_id"]
        count = source.execute("SELECT COUNT(*) FROM documents WHERE case_id=?", (case_id,)).fetchone()[0]
        if count != 1:
            raise ValueError("This case has multiple documents; one-file migration would leave broken citations")
        case = source.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        history = source.execute("SELECT * FROM audit WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        content = (root / "documents" / f"{document_id}.pdf").read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != document["sha256"]:
            raise ValueError("Selected PDF does not match the database SHA-256")
        record = json.loads(case["record"])

        def citations(value):
            if isinstance(value, dict):
                if "document_id" in value:
                    yield value["document_id"]
                for child in value.values():
                    yield from citations(child)
            elif isinstance(value, list):
                for child in value:
                    yield from citations(child)

        if any(ref != document_id for ref in citations(record)):
            raise ValueError("Case record refers to a different document")
        initialize(root, database_url=url)
        with connection(root, database_url=url) as target:
            existing = target.execute("SELECT id FROM cases WHERE id=?", (case_id,)).fetchone()
            if not existing:
                target.execute("""INSERT INTO cases
                    (id,record,version,review_status,created_at,reviewed_by,reviewed_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (case["id"], case["record"], case["version"], case["review_status"],
                     case["created_at"], case["reviewed_by"], case["reviewed_at"]))
                for item in history:
                    target.execute("""INSERT INTO audit(case_id,action,snapshot,actor,created_at)
                        VALUES(?,?,?,?,?)""",
                        (item["case_id"], item["action"], item["snapshot"], item["actor"], item["created_at"]))
            existing_doc = target.execute("SELECT sha256,content FROM documents WHERE id=?", (document_id,)).fetchone()
            if not existing_doc:
                target.execute("""INSERT INTO documents
                    (id,case_id,filename,sha256,source_url,extraction,created_at,content)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (document["id"], case_id, document["filename"], document["sha256"],
                     document["source_url"], document["extraction"], document["created_at"], content))
            elif existing_doc["sha256"] != digest or bytes(existing_doc["content"] or b"") != content:
                raise ValueError("PostgreSQL already has a different copy of this document")
        with connection(root, database_url=url) as target:
            stored = target.execute("SELECT sha256,content FROM documents WHERE id=?", (document_id,)).fetchone()
            if hashlib.sha256(bytes(stored["content"])).hexdigest() != digest:
                raise ValueError("PostgreSQL PDF verification failed")
        return {"case_id": case_id, "document_id": document_id, "sha256": digest,
                "bytes": len(content), "audit_events": len(history)}
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate one IBC PDF and its case to PostgreSQL")
    parser.add_argument("--document-id", required=True)
    args = parser.parse_args()
    result = migrate_one(args.document_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
