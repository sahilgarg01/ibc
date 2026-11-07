"""Remove this project's stored records and downloaded PDFs, retaining schema and samples."""
from backend.db import Database
import os
from pathlib import Path
from backend.db import ROOT
DATA = Path(os.environ.get('IBC_DATA_DIR', ROOT / 'data')).resolve()

def main():
    database = Database(DATA)
    database.reset_project_records()
    for folder in (DATA / 'documents', DATA / 'source_documents'):
        folder = folder.resolve()
        if folder.parent != DATA:
            raise SystemExit('Refusing to remove PDFs outside the project data directory')
        pdfs = list(folder.glob('*.pdf')) if folder.exists() else []
        for pdf in pdfs:
            pdf.unlink()
        print(f'Removed {len(pdfs)} PDFs from {folder.name}')
    database.verify_project_reset()
if __name__ == '__main__':
    main()
