import logging
from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse
from .models import Approval, SaveRecord
from .url_import import ImportRequest
from .automatic_orders import ScheduleSettings
from .db import Database

class Handlers:
    """Own the complete HTTP route and exception-handler surface."""

    def __init__(self, context):
        self.context = context

    def register(self, app):
        for error in Database.DATABASE_ERRORS:
            app.add_exception_handler(error, self.database_error)
        app.get('/api/health')(self.health)
        app.get('/api/scheduler')(self.scheduler_status)
        app.put('/api/scheduler')(self.configure_scheduler)
        app.post('/api/scheduler/sync', status_code=202)(self.scheduler_sync)
        app.post('/api/imports', status_code=202)(self.import_url)
        app.get('/api/imports')(self.imports)
        app.get('/api/imports/{job_id}')(self.import_status)
        app.get('/api/import-history')(self.import_history)
        app.get('/api/entities/match')(self.entities_match)
        app.get('/api/import-files')(self.import_files)
        app.get('/api/documents/{doc_id}/content')(self.document_content)
        app.get('/api/documents/{doc_id}/pages/{page_number}/image')(self.source_page_image)
        app.post('/api/documents/{doc_id}/extract-facts')(self.populate_facts)
        app.get('/api/sample')(self.sample_info)
        app.post('/api/sample/import')(self.import_sample)
        app.get('/api/sample/pdf')(self.sample_pdf)
        app.post('/api/documents')(self.upload)
        app.get('/api/catalogue')(self.unified_catalogue)
        app.get('/api/orders/stats')(self.order_stats)
        app.get('/api/orders/filters')(self.order_filters)
        app.get('/api/orders/summary')(self.order_summary)
        app.get('/api/orders')(self.list_orders)
        app.post('/api/orders/{order_id}/review')(self.review_order)
        app.get('/api/cases')(self.list_cases)
        app.get('/api/cases/{case_id}')(self.case_detail)
        app.get('/api/documents/{doc_id}/pdf')(self.document_pdf)
        app.put('/api/cases/{case_id}')(self.save)
        app.post('/api/cases/{case_id}/approve')(self.approve)
        app.get('/api/analytics')(self.analytics)

    def health(self):
        return {'status': 'ok'}

    def scheduler_status(self):
        return self.context.collector.status()

    def configure_scheduler(self, payload: ScheduleSettings):
        try:
            return self.context.collector.configure(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def scheduler_sync(self, category: str | None=Query(None, max_length=100)):
        try:
            job_id = self.context.collector.trigger(category)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not job_id:
            raise HTTPException(409, 'An automatic sync is already running')
        return {'id': job_id, 'status': 'queued'}

    def import_url(self, payload: ImportRequest):
        return self.context.imports.import_url(payload)

    def imports(self):
        return self.context.imports.imports()

    def import_status(self, job_id: str):
        return self.context.imports.import_status(job_id)

    def import_history(self, page: int=Query(1, ge=1), page_size: int=Query(25, ge=1, le=100)):
        return self.context.imports.import_history(page, page_size)

    def entities_match(
        self,
        name: str=Query('', max_length=500),
        din: str | None=Query(None, pattern='^\\d{8}$'),
        cin: str | None=Query(None, pattern='^[LU]\\d{5}[A-Z]{2}\\d{4}[A-Z]{3}\\d{6}$'),
    ):
        return self.context.cases.entities_match(name, din, cin)

    def import_files(
        self,
        page: int=Query(1, ge=1),
        page_size: int=Query(25, ge=1, le=100),
        q: str=Query('', max_length=200),
        duplicate: str='all',
        job_id: str=Query('', max_length=32),
    ):
        return self.context.imports.import_files(page, page_size, q, duplicate, job_id)

    def document_content(self, doc_id: str):
        return self.context.documents.document_content(doc_id)

    def source_page_image(self, doc_id: str, page_number: int):
        return self.context.documents.source_page_image(doc_id, page_number)

    def populate_facts(self, doc_id: str):
        return self.context.documents.populate_facts(doc_id)

    def sample_info(self):
        return self.context.documents.sample_info()

    def import_sample(self):
        return self.context.documents.import_sample()

    def sample_pdf(self):
        return self.context.documents.sample_pdf()

    async def upload(self, request: Request):
        return await self.context.documents.upload(request)

    def unified_catalogue(
        self,
        page: int=Query(1, ge=1),
        page_size: int=Query(25, ge=1, le=100),
        q: str=Query('', max_length=200),
        category: str=Query('', max_length=100),
        bench: str='',
        remark: str='',
        adverse: str='',
        date_from: str='',
        date_to: str='',
        duplicate: str='all',
    ):
        return self.context.imports.unified_catalogue(
            page,
            page_size,
            q,
            category,
            bench,
            remark,
            adverse,
            date_from,
            date_to,
            duplicate,
        )

    def order_stats(self):
        return self.context.imports.order_stats()

    def order_filters(self):
        return self.context.imports.order_filters()

    def order_summary(self):
        return self.context.imports.order_summary()

    def list_orders(
        self,
        q: str=Query('', max_length=200),
        bench: str='',
        remark: str='',
        adverse: str='',
        category: str=Query('', max_length=100),
        date_from: str='',
        date_to: str='',
        page: int=Query(1, ge=1),
        page_size: int=Query(25, ge=1, le=100),
    ):
        return self.context.imports.list_orders(
            q,
            bench,
            remark,
            adverse,
            category,
            date_from,
            date_to,
            page,
            page_size,
        )

    def review_order(self, order_id: str):
        return self.context.imports.review_order(order_id)

    def list_cases(self, q: str=Query('', max_length=200), status: str='all', summary: bool=False):
        return self.context.cases.list_cases(q, status, summary)

    def case_detail(self, case_id: str):
        return self.context.cases.case_detail(case_id)

    def document_pdf(self, doc_id: str, download: bool=False):
        return self.context.documents.document_pdf(doc_id, download)

    def save(self, case_id: str, payload: SaveRecord):
        return self.context.cases.save(case_id, payload)

    def approve(self, case_id: str, payload: Approval):
        return self.context.cases.approve(case_id, payload)

    def analytics(self, years: int=Query(10, ge=0, le=20)):
        return self.context.analytics.analytics(years)

    async def database_error(self, request, exc):
        logging.error('Database operation failed: %s', type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                'detail': 'Database operation failed. Check PostgreSQL availability and server logs, then retry.',
            },
        )
