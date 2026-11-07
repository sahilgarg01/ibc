"""Application settings and shared collection defaults."""
import os
from pathlib import Path
from dataclasses import dataclass
ROOT = Path(__file__).resolve().parents[1]
IBBI_BASE = 'https://ibbi.gov.in'
DEFAULT_CATEGORIES = ('supreme-court', 'high-courts', 'nclat', 'nclt', 'drat', 'drts', 'ibbi', 'ipa-rvo', 'other-courts')
MAX_UPLOAD = 15 * 1024 * 1024

@dataclass(frozen=True)
class Settings:
    storage: Path
    database_url: str

    @classmethod
    def load(cls, data_dir=None):
        storage = Path(data_dir) if data_dir is not None else Path(os.environ.get('IBC_DATA_DIR', ROOT / 'data'))
        database_url = os.environ.get('DATABASE_URL', '') if data_dir is None else ''
        return cls(storage, database_url)
