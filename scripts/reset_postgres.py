"""Explicitly reset the local ibc database while keeping the application's required schema."""
from backend.db import Database
import argparse
from backend.db import Database

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--reset',
        action='store_true',
        help='Delete all project records; keep required tables',
    )
    args = parser.parse_args()
    Database.reset_postgres(args.reset)
if __name__ == '__main__':
    main()
