"""Create the configured local project database and its tables."""
from backend.db import Database
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def main():
    Database.setup_postgres()
if __name__ == '__main__':
    main()
