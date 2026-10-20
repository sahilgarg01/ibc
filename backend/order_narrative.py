"""Extract explicit narrative facts and creditor-table rows from every order page."""
import re


AMOUNT_TOKEN = re.compile(r"(?<!\d)(?:\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)(?:/\s*-)?")
CLAIM_ROW = re.compile(
    r"(?ms)^\s*(\d{1,2})\.\s+(.+?)(?=^\s*\d{1,2}\.\s+|^\s*TOTAL\b|\Z)"
)


def _add_fact(record, field, name, document_id, page, evidence, excerpt):
    name = " ".join(name.split()).strip(" ,.;:-")
    if not 2 <= len(name) <= 120:
        return
    if any(fact["value"].casefold() == name.casefold() for fact in record[field]):
        return
    record[field].append({"value": name, "document_id": document_id, "page": page})
    evidence.append({"field": field, "page": page, "excerpt": excerpt[:1000]})


def enrich(record, extraction, document_id, put, claim_rows, evidence, money, iso_date, category):
    """Populate only facts whose role and source page are explicit in the order."""
    for page in extraction["pages"]:
        number = page["page"]
        text = page["text"]
        flat = " ".join(text.split())

        if not record["professional"]:
            rp = re.search(
                r"\b(?:Mr\.?|Ms\.?)\s+([A-Z][A-Za-z .]{3,70}?)\s*,?\s*"
                r"\(hereinafter referred to as the .{0,80}?Applicant/Resolution Professional",
                flat, re.I,
            )
            if rp:
                put("professional", rp.group(1), number, rp.group(0))
        if not record["applicant"]:
            applicant = re.search(
                r"Resolution Plan,?\s+submitted by\s+(.{3,110}?)\s*,?\s+the\s+"
                r"Successful Resolution Applicant\b",
                flat, re.I,
            )
            if applicant:
                put("applicant", applicant.group(1), number, applicant.group(0))
        if not record["admission_date"]:
            admission = re.search(
                r"\border dated\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})"
                r".{0,160}?admitted the Corporate Debtor\b",
                flat, re.I,
            )
            if admission:
                put("admission_date", iso_date(admission.group(1)), number, admission.group(0))
        if not record["industry"]:
            business = re.search(
                r"\bCorporate Debtor.{0,400}?\bis engaged in\s+([^.]{5,120})\.",
                flat, re.I,
            )
            if business:
                put("industry", business.group(1), number, business.group(0))
        for field, label in (
            ("fair_value", "Fair Value"),
            ("liquidation_value", "Liquidation Value"),
        ):
            if not record[field]:
                value = re.search(
                    rf"\b{label} of the Corporate Debtor.{{0,180}}?Rs\.?\s*([\d,]+(?:\.\d+)?)/\s*-",
                    flat, re.I,
                )
                if value:
                    put(field, money(value.group(1)), number, value.group(0))

        creditor = re.search(
            r"\bwith\s+([A-Z][A-Za-z .&-]{3,100}?Limited)\s+as\s+the\s+"
            r"(?:Sole|only)\s+Financial Creditor\b",
            flat, re.I,
        )
        if creditor:
            _add_fact(record, "financial_creditors", creditor.group(1),
                      document_id, number, evidence, creditor.group(0))
        bidders = re.search(
            r"\bResolution Plans received from\s+(.{10,260}?)\.\s+The CoC\b",
            flat, re.I,
        )
        if bidders:
            for name in re.split(r"\s*,\s*|\s+and\s+", bidders.group(1)):
                _add_fact(record, "plan_submitters", name, document_id,
                          number, evidence, bidders.group(0))
        if record["applicant"] and not record["plans"]:
            planned = re.search(
                r"\bResolution Plan provides for payment of\s+Rs\.?\s*"
                r"([\d,]+(?:\.\d+)?)/\s*-\s+to\s+stakeholders",
                flat, re.I,
            )
            if planned:
                amount = money(planned.group(1))
                if amount is not None:
                    record["plans"].append({
                        "applicant": record["applicant"]["value"], "amount": amount,
                        "admitted": None, "document_id": document_id, "page": number,
                    })
                    evidence.append({"field": "plans", "page": number,
                                     "excerpt": planned.group(0)})

        # The creditor list is explicitly introduced as claims received and
        # admitted. Keep category totals distinct from individually named parties.
        if not re.search(r"list of Corporate Debtor.{0,30}creditors", flat, re.I):
            continue
        for row in CLAIM_ROW.finditer(text):
            block = " ".join(row.group(2).split())
            if not re.search(r"\bCreditors?\b", block, re.I):
                continue
            amounts = list(AMOUNT_TOKEN.finditer(block))
            if len(amounts) < 2:
                continue
            title = block[:amounts[0].start()].strip()
            if "other than Workmen" in title:
                title = "Other operational creditors"
            filed, admitted = money(amounts[0].group().replace("/-", "")), money(
                amounts[1].group().replace("/-", "")
            )
            if filed is None or admitted is None:
                continue
            claim_rows.append({
                "creditor": title[:300], "category": category(title),
                "scope": "category_total", "claimed": filed, "admitted": admitted,
                "plan_amount": None, "actual_paid": None,
                "document_id": document_id, "page": number,
                "notes": "Creditor-category total in the order; not a named party. Verify against the PDF.",
            })
            evidence.append({"field": "claims", "page": number, "excerpt": row.group(0)[:1000]})
