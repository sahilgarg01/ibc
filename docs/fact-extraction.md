# Automatic draft facts

Coram extraction reads the opening Coram section on any page, including page 2 after a cover sheet, and reconstructs wrapped names using column positions. Appearance/counsel sections are excluded. Names repeated in member-role text are deduplicated.

**Download PDF** is available in Source evidence for the currently selected document and in each Case library row. Cases with multiple PDFs expose a filename list. `/api/documents/{id}/pdf?download=true` returns an attachment with the original filename; the default URL continues to display the PDF inline. Both database-backed and local-file documents support downloads.

Shared field aliases, text normalization and financial-column names live in `backend/field_rules.py`. Add synonyms there without adding filename-specific branches. `backend/section_facts.py` applies these mappings to labeled paragraphs, Form H rows, valuation tables and creditor tables. It supports reordered columns, admitted/payment tables without a claimed column, repeated/partial headers, continued tables, extra PDF grid boundaries and explicit lakh/crore units. Percentage columns are excluded from monetary column matching. A missing amount remains unknown; explicit zero and Nil are zero.

`backend/amount_units.py` resolves explicit unit declarations nearest the table. All stored money remains INR: `164.01` under an “Amount in Rs. Crores” heading becomes `1640100000.00`. Each asset preserves `fair_value_source_unit` and `liquidation_value_source_unit`; the form shows `164.01 crore` alongside the normalized INR amount. A value already stated as `1640100000` in INR is unchanged and receives no crore suffix. Units are not guessed from the size of a number. Separate average columns take precedence over individual valuer columns, and the stated valuation total is retained even when rounded per-asset averages do not add up exactly.

Claims-only tables with claimed/admitted/voting columns are supported without requiring a proposed payment column. They also populate named financial creditors. Subgroup breakdowns are excluded when their parent category total is already selected. Multiline Nil/amount cells are summed before digit-wrap repair so separate amounts cannot be concatenated into a larger number. `backend/party_extraction.py` reads numbered bidder lists across pages, explicit bidders put to vote and applicant profiles. Withdrawn appeals do not establish withdrawn bids; subsidiary names do not establish applicant directors.

Valuation cells may contain named valuers and an explicit average. A documented average takes precedence; otherwise multiple values are averaged and a single value is retained. Separate fair/liquidation source-page fields support valuation pairs split across pages. Proposed equity allotments and directors use `proposed_owners` / `proposed_directors`; they do not enter confirmed ownership analytics. An aggregate valuation card is excluded when a detailed asset breakdown is available.

Every extraction report includes `field_status` (`extracted` / `not_found`). The review form explains empty ownership groups and lists fields still needing evidence. `not_found` means no supported extraction was found, not proof that the PDF lacks the fact. The extractor cannot safely fill an applicant CIN for an individual/partnership, or named owners/directors when the order describes only a future management process.

To audit every imported PDF and write the per-document report under `data/extraction-audit/batch/`:

```powershell
.venv\Scripts\python.exe -m scripts.audit_pdf_fields
# Refresh stored draft records through the normal API transaction; approved records are skipped.
.venv\Scripts\python.exe -m scripts.audit_pdf_fields --apply
```

The apply mode backs up each original record/extraction before updating. Drafts with a complete automatic-only history and one source document are rebuilt to remove stale suggestions; manually saved, starter and approved records retain their existing values. A repeat refresh does not increment the case version when nothing changes.

Optional browser verification (install Playwright and have Microsoft Edge available): `python -m scripts.capture_review <case-id>`. This captures actual identity, valuation, payment, ownership and missing-field panels without starting the scheduler. Screenshots are local artifacts under `data/extraction-audit/screenshots/`.

PDF reading combines pypdf text, PDFium fallback, pdfplumber table cells and local OCR. Pages containing substantial images also receive OCR even when their embedded headers are readable. Original embedded text and OCR output remain available separately. OCR bounding boxes preserve spacing between table columns in `layout_text`. Page metadata identifies the selected reader. The existing **Fill missing fields from PDF** action re-reads documents saved with an older reader version. Install the updated requirements; no external AI service is required.

Form H label/value tables populate debtor identity, applicant, business, dates, valuation totals and plan amounts. Creditor-table column roles and category headings carry across page breaks; numeric cells split across lines are rejoined before parsing. Nil/absent cells remain unknown, while explicit `0.00` remains zero. Reported aggregate valuations produce an aggregate asset card when both amounts share a source page; individual asset values are not invented. Narrative role definitions distinguish RP entities from IRPs and retain full company names across lines.

The Maharashtra Bio Fertilisers order (`IA(IBC)(Plan)/25/MB/2026`) has image tables on pages 5–6 and structured Form H tables on pages 9–16. Extraction recovers a ₹19 crore plan, ₹60,54,78,854.30 admitted claims, three creditor-category rows, and page 14 fair/liquidation values of ₹2,40,028.50/₹1,62,868. The matched-creditor haircut is 69.45% using ₹18.5 crore proposed creditor payments; the ₹50 lakh CIRP allocation accounts for the rest of the plan. Proposed allocations are not actual payments.

The case-facts report includes `populated_fields` and `missing_fields` for every case-schema field except review notes. Missing values indicate that the supported extraction rules found no usable evidence; they are not invented. All supported fields continue through the shared case extractor used by upload, URL import, automatic collection and manual re-extraction.

All new manual uploads, URL imports and scheduled IBBI imports run the same case extractor and populate supported review fields immediately. It records supported case identity, labeled debtor CIN, case number, NCLT or DRT bench, dates, RP/IRP/liquidator, resolution applicant, operative approval/liquidation phrases, explicitly labeled before/after ownership and directors, claims, and plan amounts. DRT personal-guarantor orders often contain a court, IBC number and order date but no corporate resolution plan or party-wise claims; those absent fields remain empty.

Structured creditor tables identify claimed/admitted/proposed-payment columns by their headers and retain those roles across a page break. The older layout-text fallback recognizes those headers in that order with identifiable rupee units. Layout text preserves column spacing. Indian comma grouping is normalized, explicit crore/lakh units are converted to INR, and unknown values remain null. Category totals are kept separate from individual creditors. Actual payments are not inferred from plan allocations.

Plan totals come from an explicitly labeled amount or recognized table total, not an unsupported sum. A shared CIN elsewhere in a PDF is not assumed to identify the debtor. Ownership is populated only from explicit ownership/director labels; approval of an applicant's plan does not establish implemented ownership.

The bundled real sample extracts four creditor categories and the plan total on page 9, case identity and RP on page 1, IRP and admission date on page 2, and operative approval on page 20. Its applicant CIN remains separate from debtor CIN. Missing ownership is not invented.

Extraction remains a draft. The PDF extraction metadata contains `case_facts.version`, field/page excerpts and warnings. Evidence-backed facts populate the existing review form and structured database tables in the ingestion transaction. Pages with little or no embedded text now use local OCR through PDFium rendering and RapidOCR. OCR text and confidence are recorded per page; verify recognized names and amounts. Formats without supported labels or table headers may still require manual review; general-purpose AI extraction is not included.

To populate older draft PDFs, run:

```powershell
.venv\Scripts\python.exe -m scripts.populate_existing_facts
```

The backfill fills empty fields and empty lists, preserves entered values, and skips approved cases. Running it again does not add duplicate claims or increment the case version when no facts change. The review page's **Fill missing fields from PDF** button calls `POST /api/documents/{id}/extract-facts` for one document. When attaching a PDF to a draft case, automatic facts fill missing fields; existing populated claim/ownership lists are retained rather than blindly combined.
