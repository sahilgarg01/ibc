# PDF extraction with Codex SDK

The flow is PDF -> Codex SDK + JSON schema -> validated JSON -> database -> form.

`backend/codex_extraction.py` writes the original PDF to a temporary directory and connects through the official `openai-codex` Python SDK. `AsyncCodex` starts an ephemeral thread with a workspace-write sandbox and approvals denied. `thread.run(prompt, output_schema=response_schema())` returns `final_response`, which is parsed and validated. There are no application subprocess commands, CLI flags, executable-path settings, schema files, log parsers or result files.

The SDK manages its bundled runtime and uses the backend account's existing Codex authentication. Install `requirements.txt`, configure optional `CODEX_MODEL` and `CODEX_TIMEOUT_SECONDS` (default 600), and ensure the account login is available to the backend. The timeout cancels the SDK operation, and the client is always closed. Codex uses the backend Python environment's PDF tools to inspect the local file; it reports unreadable input rather than guessing.

The output schema is generated from the existing form model. It includes `record`, `subject`, `page_count`, supporting `evidence`, nullable `extraction_error`, and `field_review` for every one of the 42 form fields. Both record keys and review entries are required, including empty values. Every nested row property is required by the SDK schema. Each review states `extracted`, `not_documented` or `not_applicable`, a reason, and source excerpts with pages. Schema descriptions explain the screen meaning of every field, including ownership roles and nested financial rows. Codex must inspect the actual PDF and report an extraction error if it cannot read it. Missing facts remain null or empty. The app validates JSON, form fields and page references before storing data. Failures, timeouts or unreadable PDFs save no partial case. Temporary files are cleaned up.

`backend/main.py` saves the validated fields to `cases.record`, the complete returned JSON to `documents.extraction.response`, and the PDF bytes to `documents.content`. It projects fields into existing structured tables. The frontend fetches the saved case record from the database. Read endpoints never call Codex SDK.

**Extract again with Codex SDK** processes a stored draft PDF. An untouched single-document draft can be refreshed; entered fields are preserved. Approved cases cannot be re-extracted. Legacy saved records remain readable without invoking the SDK.

The Source evidence panel reads full page text directly from the stored PDF using Python (`backend/pdf_source.py` and pypdf). The `/api/documents/{id}/content` endpoint caches these pages separately in `document_source_evidence`; it never uses model excerpts as page text and never reruns Codex. Existing documents are read and cached on their first source view. If a page has no readable text layer, the panel displays an image of the original page rendered with Python/PDFium. No OCR text is invented. Source viewing does not change case fields, versions, approvals or the saved Codex response. Human review and approval remain part of the application.

Official OpenAI documentation: https://learn.chatgpt.com/docs/codex-sdk.

Screen totals, haircut and reconciliation indicators continue to be calculated by the application from the returned amounts. Reviewer name, review confirmation and approval status remain human inputs. Attached orders for other companies must not supply the selected debtor's facts.

To verify a real SDK extraction without changing application records, run `.\.venv\Scripts\python.exe -m scripts.verify_codex_sdk`. It processes the bundled PDF in a disposable SQLite database and checks all 42 fields and reviews plus Python source evidence. Pass `--pdf path/to/order.pdf` to check another document.
