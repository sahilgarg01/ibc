# IBC Evidence Explorer

A runnable backend and frontend proof of concept for reviewing corporate insolvency orders and comparing admitted claims with resolution plan amounts. The app uses **PostgreSQL** when `DATABASE_URL` is set in the local ignored `.env` file. SQLite remains available for isolated tests and as a backup of earlier local data.

The **IBBI order catalogue** shows public order listings and imported PDF files in one table. Listings appear before a PDF is downloaded; once available, the same row shows its stored file, content, duplicate status and case action. Manual imports also appear there. The case dashboard uses only human-approved fields because a listing alone does not establish claims, payments or ownership history.

## Run locally

Python 3.10+ is required. On this machine, from the project root:

```powershell
& 'C:\Users\Sahil\AppData\Local\Programs\Python\Python312\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

PostgreSQL 18 is installed locally. The ignored `.env` file holds the connection settings; never commit it. The `ibc` database and tables have been created, and the single selected document `0f637acc6a9843fea2f682df499de5b7` and its case were migrated. For a fresh setup, run `python -m scripts.setup_postgres` after configuring `.env`, then `python -m scripts.migrate_one --document-id <id>` for exactly one chosen SQLite document. `python -m scripts.verify_postgres` checks the migrated case and PDF through the API.

VS Code is configured to use `.venv\Scripts\python.exe` in `.vscode/settings.json`. Open the `ibc` folder itself as the workspace. If imports still show as unresolved, run **Python: Select Interpreter** in the Command Palette and select that file, then reload the window.

On another machine, use its installed Python path for the first command. Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Click **Load sample case**, review the claims and evidence on page 9, then save or approve it. The API reference is at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs). To run the tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

With PostgreSQL configured, case records, extracted page text, review history and PDF bytes are stored in PostgreSQL. A local PDF copy is also kept under `data/documents/`; the older `data/ibc.sqlite3` is retained as a backup. Set `IBC_DATA_DIR` to change the local file directory. Uploaded PDFs are limited to 15 MB and 250 pages.

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

Automatic IBBI collection is controlled from **Document imports**. Click **Sync now** for a manual run or enable the scheduler for recurring runs. The order catalogue combines source and file fields through the queryable `ibbi_order_catalogue` database view; normalized `source_orders`, `documents` and `import_files` tables remain authoritative. PDFs exceeding the 50 MB download cap are recorded as failures. Extracted case facts remain drafts until reviewed.

## Official sample

The included [resolution approval order](samples/indo-global-resolution-order.pdf) concerns **Indo Global Soft Solutions and Technologies Private Limited**, pronounced by NCLT Mumbai Court IV on 25 November 2025. It is 23 pages long. Its [official IBBI PDF](https://ibbi.gov.in/uploads/order/e3fb9fd83bebfde5b121ca0bccbbc2f6.pdf) is listed on the [IBBI company orders page](https://ibbi.gov.in/claims/order-process/U72900PN2005PTC021732).

The IBBI download response included a short HTML loader before `%PDF-`. The exact response is retained as `samples/official-response.bin`; the bundled PDF removes those 369 prefix bytes so PDF readers accept it. [Sample metadata](samples/manifest.json) records both SHA-256 hashes and the transformation. [Starter data](samples/starter-record.json) is a manually transcribed **draft**, with page references. The page 9 table supplies four creditor-category totals; the order does not supply all requested party-wise claims, actual payments or verified past owners. The starter record leaves those fields unknown. Its computed haircut refers only to the nominal amounts in the table, subject to the order's plan conditions.

Uploads and automatic imports populate draft case fields from explicit PDF evidence, including recognized financial tables, party/role labels, dates, ownership labels and plan amounts. Each value retains a document/page reference. Existing entered values take precedence; approved records are preserved. Scanned or unsupported layouts still require OCR/manual review. See [fact extraction](docs/fact-extraction.md).

See [architecture](docs/architecture.md) for the components, data rules and expansion path.

## Import PDFs from a public URL

**Automatic collection:** Open Document imports and use **Sync now** or **Enable scheduler** to collect all supported IBBI order categories. The scheduler saves checkpoints and last successful sync dates, skips stored orders, and retries failures. Manual uploads remain available with Add document. See [automatic collection guide](docs/automatic-collection.md) for operation, duplicate rules and limitations.

Open **Document imports**, expand **Import an additional URL manually**, paste a page/PDF link and start the import. Progress and failures appear in the task table. Imported files appear in the unified **IBBI order catalogue**; review drafts before approving them for analytics.

PostgreSQL now includes separate case details, claims, people, plans, entities and import-job tables. Existing records are backfilled on startup. See [URL import and research guide](docs/url-import.md) for limits, matching rules, analytics formulas and APIs.

The **Research analytics** panel defaults to the last 10 years of dated, approved cases. It reports party-wise filed/admitted claim coverage, plan and payment coverage, IRP/RP-associated case haircuts with supporting case links, bench timelines, and before/after owner or director overlaps. A repeated 50%+ haircut flag requires at least two reviewed cases; it describes association, not responsibility for the plan. Undated approved cases are counted separately rather than silently included. Use **Add document → Add to case** to attach a related creditor list or later order to an existing case for review; facts absent from a document remain unknown.
