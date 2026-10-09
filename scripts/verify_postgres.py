"""Read-only check of the migrated case through the running application API."""

import hashlib

from fastapi.testclient import TestClient

from backend.main import app

DOCUMENT_ID = "0f637acc6a9843fea2f682df499de5b7"


def main():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        cases = client.get("/api/cases").json()
        assert cases, "Expected the migrated sample case in PostgreSQL"
        details = [client.get(f"/api/cases/{case['id']}").json() for case in cases]
        document = next(doc for detail in details for doc in detail['documents'] if doc['id'] == DOCUMENT_ID)
        pdf = client.get(f"/api/documents/{DOCUMENT_ID}/pdf")
        assert pdf.status_code == 200
        assert hashlib.sha256(pdf.content).hexdigest() == document["sha256"]
        assert client.get('/api/analytics').status_code == 200
        print("PostgreSQL API verified: migrated document, matching PDF hash and analytics")


if __name__ == "__main__":
    main()
