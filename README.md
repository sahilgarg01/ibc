# IBC Evidence Explorer

A runnable backend and frontend proof of concept for reviewing corporate insolvency orders and comparing admitted claims with resolution plan amounts. The app uses **PostgreSQL** when `DATABASE_URL` is set in the local ignored `.env` file. SQLite remains available for isolated tests and as a backup of earlier local data.

The **IBBI order catalogue** shows public order listings and imported PDF files in one table. Listings appear before a PDF is downloaded; once available, the same row shows its stored file, content, duplicate status and case action. Manual imports also appear there. The case dashboard uses only human-approved fields because a listing alone does not establish claims, payments or ownership history.

## Run locally

Install Codex CLI and authenticate with `codex login` under the same operating-system account that runs this backend. The ignored `.env` file contains the local configuration:

```dotenv
DATABASE_URL=postgresql://user:password@localhost:5432/ibc
CODEX_CLI_PATH=codex
CODEX_MODEL=
CODEX_TIMEOUT_SECONDS=600
```

`CODEX_CLI_PATH` is the native executable name or absolute path. On Windows, use `codex.exe`; if PATH resolves to a `.cmd` wrapper, set the full path to the native executable instead. `CODEX_MODEL` is optional and defaults to your CLI configuration. This application no longer calls the OpenAI API directly or requires `OPENAI_API_KEY`.

PostgreSQL is used when `DATABASE_URL` is set; otherwise the app uses SQLite. Install requirements in the backend environment; `pypdf` and `pypdfium2` are PDF tools Codex can use while reading the file, rather than application extraction rules.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and upload a PDF. The backend creates a temporary folder containing the original PDF and the form JSON schema, then runs `codex exec --output-schema schema.json --output-last-message result.json` there. Codex reads the PDF path using local tools and returns JSON. The CLI has no PDF attachment flag. Its JSON response is validated and saved with the case fields and PDF in the database. The form then loads those saved fields through `GET /api/cases/{id}`. Opening a case or viewing saved content never calls Codex CLI. Uploads are limited to 15 MB; import downloads are limited to 50 MB. Documents must have at most 250 physical pages.

Missing fields remain empty. Extraction failures save no partial case. Existing documents and saved records remain available. **Extract again with Codex CLI** explicitly processes a stored draft PDF again; entered fields retain precedence, while an untouched single-document draft can be refreshed. Duplicate uploads reuse the existing record without another CLI extraction. The sample import also uses Codex CLI.

Run tests with `.\.venv\Scripts\python.exe -m pytest -q`. Tests mock the Codex CLI process and use isolated SQLite databases.

## Import the public IBBI order catalogue

Run these from the project root in a **separate terminal** while the app is running:

```powershell
# See how much has been collected
.\.venv\Scripts\python.exe -m backend.ibbi_orders status

# Import all NCLT listing pages; rerun to resume missed pages
.\.venv\Scripts\python.exe -m backend.ibbi_orders sync

# Optional: enrich the stored rows with one of the site's NCLT bench filters
.\.venv\Scripts\python.exe -m backend.ibbi_orders sync --bench 38

# Optional: tag results from the site's adverse-against radio filter
.\.venv\Scripts\python.exe -m backend.ibbi_orders sync --adverse ip

# Download 25 pending official PDFs; rerun to continue
.\.venv\Scripts\python.exe -m backend.ibbi_orders sync-pdfs --limit 25

# Reattempt PDFs recorded as failed after checking the error
.\.venv\Scripts\python.exe -m backend.ibbi_orders sync-pdfs --limit 25 --retry-failed
```

`sync` reads the public GET form and every pagination page. It records the page number, fetch time, date, subject, remark, bench when a bench filter was used, and the official PDF URL. It detects duplicate PDF URLs and checkpoints each page. Failed listing pages remain visible in `status` and are retried on the next run. Use `--refresh` to revisit older pages after the site changes, or `--max-pages 5` for a short test. `sync-pdfs` stores normalized PDFs and their hashes, bytes and download status. It records download errors instead of silently treating them as complete. `follow-pdfs` is available for a long-running PDF queue after you start the listing sync. Check `status` and the logs before claiming the collection is complete.

Automatic IBBI collection is controlled from **Document imports**. PDFs are sent to Codex CLI for fields and outcome classification. Imports from the official order catalogue require a supported final outcome; other orders remain available for source review. Listing metadata alone does not fill case facts. Codex CLI failures are recorded for retry. Existing imported documents and saved records are retained. PDFs exceeding the 50 MB sync/download cap are recorded as failures; manual uploads retain their 15 MB limit. Extracted cases remain drafts until reviewed.

## Official sample

The included [resolution approval order](samples/indo-global-resolution-order.pdf) concerns **Indo Global Soft Solutions and Technologies Private Limited**, pronounced by NCLT Mumbai Court IV on 25 November 2025. It is 23 pages long. Its [official IBBI PDF](https://ibbi.gov.in/uploads/order/e3fb9fd83bebfde5b121ca0bccbbc2f6.pdf) is listed on the [IBBI company orders page](https://ibbi.gov.in/claims/order-process/U72900PN2005PTC021732).

The IBBI download response included a short HTML loader before `%PDF-`. The exact response is retained as `samples/official-response.bin`; the bundled PDF removes those 369 prefix bytes so PDF readers accept it. [Sample metadata](samples/manifest.json) records both SHA-256 hashes and the transformation. [Starter data](samples/starter-record.json) is a manually transcribed **draft**, with page references. The page 9 table supplies four creditor-category totals; the order does not supply all requested party-wise claims, actual payments or verified past owners. The starter record leaves those fields unknown. Its computed haircut refers only to the nominal amounts in the table, subject to the order's plan conditions.

Uploads and imports use `backend/codex_extraction.py` for all PDF field extraction. The old application OCR, table parsers and field rules remain removed. The model returns the existing form schema, supporting excerpts and physical page references. The review screen displays saved excerpts; use the original PDF to inspect the full source. See [fact extraction](docs/fact-extraction.md).

See [architecture](docs/architecture.md) for the components, data rules and expansion path.

## Import PDFs from a public URL

**Automatic collection:** Open Document imports and use **Sync now** or **Enable scheduler** to collect all supported IBBI order categories. The scheduler saves checkpoints and last successful sync dates, skips stored orders, and retries failures. Manual uploads remain available with Add document. See [automatic collection guide](docs/automatic-collection.md) for operation, duplicate rules and limitations.

Open **Document imports**, expand **Import an additional URL manually**, paste a page/PDF link and start the import. Progress and failures appear in the task table. Imported files appear in the unified **IBBI order catalogue**; review drafts before approving them for analytics.

PostgreSQL now includes separate case details, claims, people, plans, entities and import-job tables. Existing records are backfilled on startup. See [URL import and research guide](docs/url-import.md) for limits, matching rules, analytics formulas and APIs.

The **Research analytics** panel defaults to the last 10 years of dated, approved cases. It reports party-wise filed/admitted claim coverage, plan and payment coverage, IRP/RP-associated case haircuts with supporting case links, bench timelines, and before/after owner or director overlaps. A repeated 50%+ haircut flag requires at least two reviewed cases; it describes association, not responsibility for the plan. Undated approved cases are counted separately rather than silently included. Use **Add document → Add to case** to attach a related creditor list or later order to an existing case for review; facts absent from a document remain unknown.

To verify real final orders through both import methods without changing application data, run `.\.venv\Scripts\python.exe -m scripts.verify_final_imports`. This downloads two public PDFs into memory and verifies resolution/liquidation review drafts in disposable SQLite databases.

Codex CLI extraction uses a temporary workspace with the workspace-write sandbox and no interactive approval prompts. The PDF, schema, intermediate files and CLI output are cleaned up after each run. Successful JSON is retained in `documents.extraction.response`; validated fields are stored in `cases.record` and projected into the structured tables. Failed, timed-out, unreadable or invalid results do not create a partial case.

If the CLI cannot find its home directory or login, set a valid `CODEX_HOME` for the backend account and run `codex login` there before starting the server. The backend does not create or modify your Codex authentication configuration.
