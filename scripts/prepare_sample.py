"""Prepare a traceable local PDF and manually curated draft, not automatic extraction."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
pdf = SAMPLES / "indo-global-resolution-order.pdf"
raw_path = SAMPLES / "official-response.bin"
raw = raw_path.read_bytes() if raw_path.exists() else pdf.read_bytes()
raw_path.write_bytes(raw)
offset = raw.find(b"%PDF-")
if offset < 0:
    raise ValueError("Official response contains no PDF")
# IBBI prepended a 369-byte HTML loader. Preserve original bytes separately.
content = raw[offset:]
pdf.write_bytes(content)
manifest = {
    "title": "Indo Global Soft Solutions and Technologies Private Limited — resolution approval order",
    "filename": pdf.name,
    "source_url": "https://ibbi.gov.in/uploads/order/e3fb9fd83bebfde5b121ca0bccbbc2f6.pdf",
    "listing_url": "https://ibbi.gov.in/claims/order-process/U72900PN2005PTC021732",
    "order_date": "2025-11-25", "pages": 23,
    "sha256": hashlib.sha256(content).hexdigest(),
    "original_response_sha256": hashlib.sha256(raw).hexdigest(),
    "removed_prefix_bytes": offset,
    "normalization": "Removed the HTML prefix before %PDF-. PDF content is otherwise unchanged; original response retained as official-response.bin.",
    "fixture_method": "Manually transcribed draft with page references. Generic uploads extract text and candidate CINs only."
}
(SAMPLES / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def fact(value, page):
    return {"value": value, "document_id": "SAMPLE_DOCUMENT", "page": page}


rows = [
    ("Secured financial creditors (aggregate)", "secured_financial", "1738568031", "1724612739", "1300000000"),
    ("Unsecured financial creditors (aggregate)", "unsecured_financial", "10355083287", "6907816278", "151200000"),
    ("Operational creditors (aggregate)", "operational", "228991461", "198761621", "1200000"),
    ("Other creditors (aggregate)", "other", "24102085857", "120000000", "200000"),
]
record = {
    "company": fact("Indo Global Soft Solutions and Technologies Private Limited", 1),
    "case_number": fact("C.P. (IB) No. 377/MB/2021; IA (IBC)(Plan) No. 86 of 2025", 1),
    "tribunal": fact("NCLT Mumbai, Court IV", 1),
    "order_date": fact("2025-11-25", 1),
    "irp": fact("Shailen Shah (initial IRP)", 2),
    "professional": fact("Ravi Sethia (RP at plan approval)", 1),
    "applicant": fact("Ashdan Properties Private Limited", 1),
    "outcome": "resolution_approved",
    "outcome_evidence": fact("Resolution plan approved", 20),
    "claims": [{"creditor": name, "category": category, "scope": "category_total", "claimed": claimed,
                "admitted": admitted, "plan_amount": planned, "actual_paid": None,
                "document_id": "SAMPLE_DOCUMENT", "page": 9,
                "notes": "Page 9 financial proposal; nominal scheduled amount, not proof of cash received. Subject to plan provisions and costs."}
               for name, category, claimed, admitted, planned in rows],
    "notes": "Manually prepared starter draft. Four category totals, not party-wise claims. The plan also addresses costs, cash balances, equity and contingent recoveries; this POC ratio uses the page 9 nominal payment table only. Actual payments and implemented ownership/directors are unknown. The CIN on page 8 belongs to the successful applicant, not the debtor. RP changes appear on page 6. Review against the original order before approval."
}
(SAMPLES / "starter-record.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
print(json.dumps(manifest, indent=2))
