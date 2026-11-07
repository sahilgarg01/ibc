"""Copy one reviewed document and its one-document case from SQLite to PostgreSQL."""
from backend.db import Database
migrate_one = Database.migrate_one
import argparse
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description='Migrate one IBC PDF and its case to PostgreSQL')
    parser.add_argument('--document-id', required=True)
    args = parser.parse_args()
    result = migrate_one(args.document_id)
    print(json.dumps(result, indent=2))
if __name__ == '__main__':
    main()
