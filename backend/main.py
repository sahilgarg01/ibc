"""FastAPI composition and application lifecycle."""
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from .logging_config import configure_logging
from .config import ROOT, Settings
from .db import Database
from .handlers import Handlers
from .case_service import CaseService
from .document_service import DocumentService
from .import_service import ImportService
from .research_analytics import ResearchAnalytics
from .automatic_orders import AutomaticOrders

class Application:

    def __init__(self, data_dir=None):
        configure_logging()
        self.settings = Settings.load(data_dir)
        self.database = Database(self.settings.storage, self.settings.database_url)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pdf-import')
        self.import_slots = BoundedSemaphore(3)
        self.cases = CaseService(self)
        self.documents = DocumentService(self)
        self.imports = ImportService(self)
        self.analytics = ResearchAnalytics(self)
        self.collector = AutomaticOrders(self.database.session, self.ingest_order)
        self.handlers = Handlers(self)
        self.app = FastAPI(title='IBC Evidence Explorer', version='0.1.0', lifespan=self.lifespan)
        self.app.state.application = self
        self.handlers.register(self.app)
        self.app.mount('/', StaticFiles(directory=ROOT / 'frontend', html=True), name='frontend')

    def ingest_order(self, content, filename, url, extraction, decision):
        return self.documents.ingest(
            content,
            filename,
            url,
            max_bytes=50 * 1024 * 1024,
            preextracted=extraction,
        )

    @asynccontextmanager
    async def lifespan(self, app):
        self.database.initialize()
        with self.database.session() as database:
            database.interrupt_orphaned_jobs(Database.timestamp())
        self.collector.start()
        try:
            yield
        finally:
            self.collector.close()
            self.executor.shutdown(wait=True)

def create_app(data_dir: Path | None=None):
    return Application(data_dir).app
app = create_app()
