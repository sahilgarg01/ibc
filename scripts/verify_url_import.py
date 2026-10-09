"""Exercise a single official PDF through the URL import API and PostgreSQL."""
import json
import time

from fastapi.testclient import TestClient
from backend.main import ROOT, app


def main():
    manifest = json.loads((ROOT / "samples/manifest.json").read_text(encoding="utf-8"))
    with TestClient(app) as client:
        response = client.post("/api/imports", json={"url": manifest["source_url"], "max_pages": 1, "max_files": 1})
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        for _ in range(120):
            job = client.get("/api/imports/" + job_id).json()
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(.5)
        result = job["result"]
        assert job["status"] == "completed", result
        assert result["duplicates"] == 1, result
        print("Live public PDF import verified: existing PostgreSQL PDF recognized as duplicate")


if __name__ == "__main__":
    main()
