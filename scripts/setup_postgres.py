"""Create the configured local project database and its tables."""

import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg import sql

from backend.db import initialize

ROOT = Path(__file__).resolve().parents[1]


def main():
    url = os.environ.get("DATABASE_URL", "")
    parsed = urlparse(url)
    name = parsed.path.lstrip("/")
    if parsed.scheme not in {"postgresql", "postgres"} or not name or "/" in name:
        raise SystemExit("Set DATABASE_URL in .env to a PostgreSQL database URL first")
    admin_url = parsed._replace(path="/postgres").geturl()
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=10) as db:
        exists = db.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
        if not exists:
            db.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(sql.Identifier(name)))
            print(f"Created database {name}")
        else:
            print(f"Database {name} already exists")
    initialize(ROOT / "data", database_url=url)
    print("PostgreSQL tables ready")


if __name__ == "__main__":
    main()
