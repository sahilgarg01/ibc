"""Verify catalogue APIs against configured PostgreSQL without changing data."""
from fastapi.testclient import TestClient
from backend.main import app


def main():
    with TestClient(app) as client:
        for path in ('/api/orders', '/api/orders/stats', '/api/orders/filters', '/api/orders/summary', '/api/orders?q=sample', '/api/import-files', '/api/import-files?duplicate=yes', '/api/import-history', '/api/import-files?job_id=nonexistent', '/api/scheduler', '/api/orders?category=nclt'):
            response = client.get(path)
            print(path, response.status_code)
            assert response.status_code == 200, response.text


if __name__ == '__main__':
    main()
