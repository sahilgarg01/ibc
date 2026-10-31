# PDF extraction with Codex CLI

The flow is PDF -> Codex CLI + JSON schema -> validated JSON -> database -> form.

`backend/codex_extraction.py` creates a temporary workspace containing `document.pdf` and `schema.json`. It runs the native Codex executable with `exec`, `--output-schema` and `--output-last-message`. The prompt names the local PDF and the backend Python executable. Codex uses PDF tools (`pypdf` and `pypdfium2`, installed with requirements) to read text or render scanned pages, then returns JSON. The CLI has no native PDF attachment flag; the file path is supplied in the prompt. The app has no PDF field rules or OCR extraction pipeline.

Authenticate with `codex login` as the backend user. Configure `CODEX_CLI_PATH` (default `codex`), optional `CODEX_MODEL` (default CLI configuration), and `CODEX_TIMEOUT_SECONDS` (default 600). On Windows point to the native `codex.exe`, not a `.cmd` wrapper. The backend makes no direct OpenAI API request and does not need an API key. A valid Codex home and login must be available to the server process.

The output schema is generated from the existing form model. It includes `record`, `subject`, `page_count`, supporting `evidence`, nullable `extraction_error`, and `field_review` for every one of the 42 form fields. Both record keys and review entries are required, including empty values. Every nested row property is required by the CLI schema. Each review states `extracted`, `not_documented` or `not_applicable`, a reason, and source excerpts with pages. Schema descriptions explain the screen meaning of every field, including ownership roles and nested financial rows. Codex must inspect the actual PDF and report an extraction error if it cannot read it. Missing facts remain null or empty. The app validates JSON, form fields and page references before storing data. Failures, timeouts or unreadable PDFs save no partial case. Temporary files are cleaned up.

`backend/main.py` saves the validated fields to `cases.record`, the complete returned JSON to `documents.extraction.response`, and the PDF bytes to `documents.content`. It projects fields into existing structured tables. The frontend fetches the saved case record from the database. Read endpoints never call Codex CLI.

**Extract again with Codex CLI** processes a stored draft PDF. An untouched single-document draft can be refreshed; entered fields are preserved. Approved cases cannot be re-extracted. Legacy saved records remain readable without invoking the CLI.

The Source evidence panel reads full page text directly from the stored PDF using Python (`backend/pdf_source.py` and pypdf). The `/api/documents/{id}/content` endpoint caches these pages separately in `document_source_evidence`; it never uses model excerpts as page text and never reruns Codex. Existing documents are read and cached on their first source view. If a page has no readable text layer, the panel displays an image of the original page rendered with Python/PDFium. No OCR text is invented. Source viewing does not change case fields, versions, approvals or the saved Codex response. Human review and approval remain part of the application.

Official OpenAI documentation: https://learn.chatgpt.com/docs/non-interactive-mode.

Screen totals, haircut and reconciliation indicators continue to be calculated by the application from the returned amounts. Reviewer name, review confirmation and approval status remain human inputs. Attached orders for other companies must not supply the selected debtor's facts.
