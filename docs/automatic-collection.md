# Automatic IBBI order collection

Open **Document imports**. Click **Sync now** for one complete collection, or **Enable scheduler** to start recurring automatic collection. The default interval is 1,440 minutes (daily). Save another interval if desired; the minimum is 15 minutes. **Add document** remains at the top right for manual uploads; additional manual URL imports are under the expandable section.

The collector seeds Supreme Court, High Courts, NCLAT, NCLT, DRAT, DRTs, IBBI, IPA/RVO and Other Courts. Each run also discovers further `/orders/` categories linked in IBBI's order menu. It imports PDFs linked by public order listing tables, follows pagination, creates catalogue metadata and stores original PDF bytes (after removing any IBBI HTML loader prefix). Tasks appear in **Document imports**; orders and imported files appear together in **IBBI order catalogue**.

Each category stores its last successful sync, page checkpoint, status and error. The first successful run traverses historical pages. Subsequent runs scan listing metadata and skip known PDFs that were already stored successfully with unchanged listing subject/date. New URLs, changed metadata and previous failures are downloaded. This uses a saved sync baseline rather than assuming order date equals publication date: IBBI's date form is an exact-date filter, not a verified publication-since filter. Rescanning metadata also catches newly posted older orders. Successful sync timestamps do not advance when a category has failed downloads or failed pages. Initial failed-page runs resume from their checkpoint; failed PDFs are retried.

The scheduler setting and progress are stored in PostgreSQL. A renewable database lease prevents overlapping automatic runs, including across workers. The application must stay running for its scheduler to operate. After a crash, an expired lease allows the next run to resume. Disabling the scheduler prevents future scheduled runs; an active run finishes. Closing the application signals the collector to stop and preserves its checkpoints. Run without development reload during a historical collection:

The scheduler is disabled initially. Enable it from the screen when ready to start the full historical collection. Live checks verify the first-page parsers; these checks do not themselves download the historical archive.

```powershell
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

No Windows service or Windows Task Scheduler task is installed by this change. For collection while the IDE is closed, keep the application running as a service or on a server.

## Duplicate and related-document checks

- Matching PDF SHA-256 is a confirmed exact duplicate. Reuse the stored file and record the import attempt.
- Matching extracted text, filename, subject, or same-day ingestion with a matching filename creates a **possible match**, displayed with the candidate file and download link. Both differing PDFs are retained.
- Shared CIN or explicitly labelled DIN creates a related-document candidate. This does not prove duplication or the same proceeding; identifiers can refer to an applicant or another party.
- Blank or very short extracted text is never used as a content fingerprint.

Fingerprint and identifier tables are indexed. Existing PDFs are backfilled at startup. Only the last 20 automatic import items are kept as a preview in job JSON; all file attempts persist in `import_files` and remain available through the job's paginated file table. Known orders are skipped without re-extracting their PDFs. The old CLI collector remains available for existing workflows; the scheduler reuses its listing parser rather than duplicating it.

Automatic PDF downloads are limited to 50 MB and the current text extractor accepts up to 250 pages. Unreadable, encrypted, oversized or otherwise unsupported files are recorded as failures and do not silently advance the successful sync date. Scanned PDFs are stored when readable, but their fields still need OCR/review. CAPTCHA, access restrictions and changes to IBBI's layout appear as errors; the collector does not bypass them. Extracted cases remain drafts until reviewed.

APIs: `GET /api/scheduler`, `PUT /api/scheduler` with `{"enabled":true,"interval_minutes":1440}`, and `POST /api/scheduler/sync`. Automatic jobs use the existing import-history and import-files APIs.
