# Public URL imports and research data

Start the app with `.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000` and open http://127.0.0.1:8000.

Click **Import from URL**, paste a public listing URL or direct PDF URL, choose page/file limits, and click **Start import**. Progress includes imported files, duplicates, failed files, and individual errors. Open a resulting draft with its Review button. The latest import is available when reopening the dialog; recent history is also available at `GET /api/imports`.

Leave **Maximum listing pages** empty to follow discoverable pagination until the PDF file limit is reached, or enter a number to limit pages. Reaching either limit ends the import and sets its completion status. The file limit counts attempted distinct PDF URLs, including duplicates and failures. This cannot discover pages hidden behind unsupported JavaScript or forms.

Open **IBBI order catalogue** for a searchable, paginated table with source order fields, duplicate status, timestamps, extracted content, case review and PDF open/download actions. Each IBBI listing has one row, linked to its stored document when downloaded. Non-IBBI URL import attempts and local uploads also appear. Exact duplicates point to the original stored document. Failed downloads remain in the import job's error list. The combined database view is `ibbi_order_catalogue`; source, document and import-event tables retain the authoritative records.

The crawler follows PDF anchors, embedded PDFs and ordinary same-site pagination. It validates public HTTP/HTTPS destinations including redirect targets, limits downloads to 15 MB, retries temporary network failures, and deduplicates PDF content by SHA-256. It does not execute JavaScript or submit arbitrary filter forms. A page requiring CAPTCHA, login, or dynamic form interaction needs a source-specific adapter. This is a bounded import, not an exhaustive crawl of an entire website. IBBI's dedicated catalogue sync remains available separately.

IBBI may prepend an HTML loader to a PDF response. The importer removes a prefix when the PDF header appears within the first 8 KB and records the removed byte count in the job item. Deduplication uses the resulting PDF bytes. A live check against the bundled sample's official URL confirmed duplicate detection in PostgreSQL.

Imports run on one background worker with at most three queued/running jobs. Jobs and results persist in PostgreSQL; after a server restart unfinished jobs are marked interrupted. Retry the URL to continue; stored content is deduplicated. Run one application process for this local POC. This is not a distributed job queue. Stop an active import before a development reload if you want uninterrupted progress.

## Database

Existing evidence JSON and audit history remain intact. `case_details` has one row per case with company, CIN, bench, admission/resolution dates, IRP, RP and outcome. `claims` stores party/category, filed, admitted, plan amount and actual payment. `people` stores owners/directors before and after, DIN/CIN, and resolved entity ID. `plans` stores applicant, amount, denominator and calculated haircut. `entities` stores shared DIN/CIN identities. `import_jobs` stores import history. Every populated claim, person and plan retains document/page evidence. Decimal financial columns use NUMERIC in PostgreSQL. Tables are created and existing records backfilled on application startup; future saves update the projection in the same transaction.

One PDF does not always equal one legal case. Unknown imported PDFs start as separate drafts. Use the existing Add document case selector to attach additional documents to a known case. Case identity and document-to-case grouping require review; CIN identifies a company, not a unique proceeding. There is no automatic case merge.

Generic import extracts PDF text and candidate CINs; it does not reliably infer every financial or ownership fact. CIN candidates can belong to an applicant rather than the debtor. Complete the review fields against the original pages, including new dates, DIN/CIN and plans. Scanned PDFs still need OCR outside this implementation. Unknown financial values remain null rather than becoming zero.

## Matching and analytics

Use **Find matching entities** with DIN or CIN for confirmed identity lookup. Matching IDs share an entity across cases and document mentions even when names differ. Name-only searches return similarity candidates for review, never automatic merges. DIN/CIN on ownership rows must be supported by the referenced evidence page.

Analytics include approved cases only. IRP/RP averages are arithmetic averages of case haircuts, separate from portfolio weighted haircut. Creditor-matched admitted and planned amounts supply case haircuts; if these are unavailable and exactly one plan has both amount and admitted denominator, professional analytics use that plan. Multiple plans are not blindly summed. Professionals currently group by their reviewed names; spelling variants of professional names should be reconciled during review.

Bench timelines measure calendar days from admission to resolution and exclude missing dates. Ownership overlap confirms shared DIN/CIN; equal names without IDs appear as candidates, not confirmed overlap. Missing ownership data cannot prove that ownership changed.

API: `POST /api/imports` accepts `{ "url": "https://ibbi.gov.in/orders/nclt", "max_pages": 5, "max_files": 25 }` and returns a job ID with HTTP 202. Poll `GET /api/imports/{id}`. `GET /api/entities/match?din=12345678` supports identity lookup, and `GET /api/analytics` includes professional, bench and overlap results.
