# Automatic draft facts

All new manual uploads, URL imports and scheduled IBBI imports run the same case extractor and populate supported review fields immediately. It records supported case identity, labeled debtor CIN, case number, NCLT or DRT bench, dates, RP/IRP/liquidator, resolution applicant, operative approval/liquidation phrases, explicitly labeled before/after ownership and directors, claims, and plan amounts. DRT personal-guarantor orders often contain a court, IBC number and order date but no corporate resolution plan or party-wise claims; those absent fields remain empty.

Recognized tables require claimed/admitted/proposed-payment headers in that order and identifiable rupee units. Layout text preserves column spacing and allows a table to continue on the next page. Indian comma grouping is normalized, explicit crore/lakh units are converted to INR, and unknown values remain null. Category totals are kept separate from individual creditors. Actual payments are not inferred from plan allocations.

Plan totals come from an explicitly labeled amount or recognized table total, not an unsupported sum. A shared CIN elsewhere in a PDF is not assumed to identify the debtor. Ownership is populated only from explicit ownership/director labels; approval of an applicant's plan does not establish implemented ownership.

The bundled real sample extracts four creditor categories and the plan total on page 9, case identity and RP on page 1, IRP and admission date on page 2, and operative approval on page 20. Its applicant CIN remains separate from debtor CIN. Missing ownership is not invented.

Extraction remains a draft. The PDF extraction metadata contains `case_facts.version`, field/page excerpts and warnings. Evidence-backed facts populate the existing review form and structured database tables in the ingestion transaction. Pages with little or no embedded text now use local OCR through PDFium rendering and RapidOCR. OCR text and confidence are recorded per page; verify recognized names and amounts. Formats without supported labels or table headers may still require manual review; general-purpose AI extraction is not included.

To populate older draft PDFs, run:

```powershell
.venv\Scripts\python.exe -m scripts.populate_existing_facts
```

The backfill fills empty fields and empty lists, preserves entered values, and skips approved cases. Running it again does not add duplicate claims or increment the case version when no facts change. The review page's **Fill missing fields from PDF** button calls `POST /api/documents/{id}/extract-facts` for one document. When attaching a PDF to a draft case, automatic facts fill missing fields; existing populated claim/ownership lists are retained rather than blindly combined.
