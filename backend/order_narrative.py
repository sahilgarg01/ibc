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
        sole = re.search(r'\b(?:sole|only)\s+Financial\s+Creditor,?\s*(?:i\.e\.,?|namely,?)\s+([A-Z][A-Za-z &.-]{2,100}?)(?=\.\s|[;\n]|$)', flat, re.I)
        if sole:
            _add_fact(record, 'financial_creditors', sole[1], document_id,
                      number, evidence, sole.group())
        received = re.search(r'claims\s+were\s+received\s+from\s+(.{3,300}?),\s*which\s+were\s+verified.{0,180}?admitted\s+on\s+a\s+provisional\s+basis', flat, re.I)
        if received:
            opening = ' '.join(' '.join(p['text'].split()) for p in extraction['pages'][:3])
            for creditor in re.split(r'\s+and\s+', received[1]):
                stem = re.split(r'\b(?:Private|Pvt|Limited|Ltd)\b', creditor, maxsplit=1, flags=re.I)[0].strip()
                kind = ('government' if re.search(r'State\s+Insurance\s+Corporation|Provident\s+Fund', creditor, re.I)
                        else 'operational' if re.search(re.escape(stem) + r'.{0,45}?OPERATIONAL\s+CREDITOR', opening, re.I)
                        else 'other')
                claim_rows.append({'creditor':creditor.strip(), 'category':kind, 'scope':'individual',
                    'claimed':None, 'admitted':None, 'plan_amount':None, 'actual_paid':None,
                    'document_id':document_id, 'page':number,
                    'notes':'Claim received and provisionally admitted; no amount is quantified in this paragraph.'})
            evidence.append({'field':'claims','page':number,'excerpt':received.group()[:1000]})
        # These are amounts received, not admitted amounts or plan payments.
        # A financial creditor's security is not established by its bank name.
        financial = re.search(r'\breceived\s+one\s+claim\s+by\s+a\s+Financial\s+Creditor\s+amounting\s+to\s+(?:Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)/\s*-', flat, re.I)
        operational = re.search(r'\b\d+\s+claims\s+by\s+Operational\s+Creditors,?\s+totall?ing\s+(?:Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)/\s*-', flat, re.I)
        for match, name, kind, scope in (
                (financial, sole[1] if sole else 'Financial Creditors', 'other', 'individual' if sole else 'category_total'),
                (operational, 'Operational Creditors', 'operational', 'category_total')):
            if match and (amount := money(match[1])) is not None:
                claim_rows.append({'creditor': name, 'category': kind, 'scope': scope,
                    'claimed': amount, 'admitted': None, 'plan_amount': None,
                    'document_id': document_id, 'page': number,
                    'notes': 'Claim received as stated in narrative; admission and payment are not stated.'})
                evidence.append({'field': 'claims', 'page': number, 'excerpt': match.group()})
        bidders = re.search(
            r"\bResolution Plans received from\s+(.{10,260}?)\.\s+The CoC\b",
            flat, re.I,
        )
        if bidders:
            for name in re.split(r"\s*,\s*|\s+and\s+", bidders.group(1)):
                _add_fact(record, "plan_submitters", name, document_id,
                          number, evidence, bidders.group(0))
        if record["applicant"] and not record["plans"]:
            total = re.search(r"\btotal Resolution Plan value is\s+(?:Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)", flat, re.I)
            if total:
                record["plans"].append({"applicant": record["applicant"]["value"],
                    "amount": money(total.group(1)), "admitted": None,
                    "document_id": document_id, "page": number})
                evidence.append({"field": "plans", "page": number, "excerpt": total.group(0)})
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



def enrich_extended(record, extraction, document_id, put, claim_rows, evidence, money, iso_date, category):
    """Page-cited facts from labeled narrative and valuation/payment tables."""
    def add(field, value, page, excerpt):
        if value is not None:
            put(field, str(value), page, excerpt)
    def name(field, value, page, excerpt):
        _add_fact(record, field, value, document_id, page, evidence, excerpt)
    for page in extraction["pages"]:
        n, text = page["page"], page["text"]
        flat = " ".join(text.split())
        if n <= 2:
            for m in re.finditer(r"\bSHRI\.?\s+([A-Z][A-Z ]{4,60}?)\s*,?\s*MEMBER\s*\((JUDICIAL|TECHNICAL)\s*\)", flat, re.I):
                name("judges", f"{m.group(1).title().strip()} - Member ({m.group(2).title()})", n, m.group(0))
            if not record["professional"]:
                m = re.search(r"\b(?:Mr|Ms|Mrs)\.?\s+([A-Z][A-Za-z .]{4,70}?)\s*(?:\[[^\]]{0,130}\]\s*)?Resolution\s+Professional\s+of\b", flat, re.I)
                if m and not re.search(r'\b(?:was|appointed|Interim)\b', m.group(1), re.I):
                    put("professional", m.group(1), n, m.group(0))
        if not record["professional"]:
            m = re.search(r"\b(?:Mr|Ms|Mrs)\.?\s+([A-Z][A-Za-z .]{4,70}?)\s*,?\s*(?:the\s+)?(?:Applicant\s*/\s*)?Resolution\s+Professional\b", flat, re.I)
            if m and not re.search(r'\b(?:was|appointed|Interim)\b', m.group(1), re.I):
                put("professional", m.group(1), n, m.group(0))
        if not record["case_number"] and n <= 3 and not re.search(r"IN THE HIGH COURT", extraction["pages"][0]["text"][:1200], re.I):
            m = re.search(r"\bC\.?\s*P\.?\s*\(\s*I\.?\s*B\.?\s*\)\s*(?:No\.?\s*)?(\d{1,6})\s*(?:/|of)\s*([A-Za-z]{2,12}\s*/\s*)?(\d{4})\b", flat, re.I)
            if m: put("case_number", m.group(0), n, m.group(0))
        m = re.search(r"\bclaims?\s+(?:received|filed)\s+(?:amounting\s+to|of|aggregating\s+to)\s*(?:Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)\s*(crores?|cr\.?|lakhs?|lacs?)?", flat, re.I)
        if m:
            add("total_received_claims", money("Rs. " + m.group(1) + (" " + m.group(2) if m.group(2) else "")), n, m.group(0))
        m = re.search(r"(?m)^\s*TOTAL\s+([\d,]+)(?:/\s*-)?\s+([\d,]+)(?:/\s*-)?\s+\d+\s*$", text, re.I)
        if m:
            add("total_received_claims", money(m.group(1)), n, m.group(0))
            add("total_admitted_claims", money(m.group(2)), n, m.group(0))
        m = re.search(r"\btotal\s+(?:admitted\s+claims|claims\s+admitted)\s*[:\-]?\s*(?:Rs\.?|INR)\s*([\d,]+)", flat, re.I)
        if m: add("total_admitted_claims", money(m.group(1)), n, m.group(0))
        m = re.search(r"\b([A-Z][A-Za-z .&-]{3,100}?(?:Bank|Limited|Ltd\.?))\s+as\s+(?:the\s+)?(?:sole|only)\s+Financial\s+Creditor\s+having\s+(\d{1,3}(?:\.\d+)?)\s*%\s+voting\s+rights", flat, re.I)
        if m: add("coc_voting_share", f"{m.group(1).rsplit(" with ", 1)[-1].strip()}: {m.group(2)}%", n, m.group(0))
        m = re.search(r"\b(?:one\s+of\s+the\s+(?:PRAs|applicants),?\s+namely\s+|(?:prospective\s+)?resolution\s+applicant\s+)([A-Z][A-Za-z0-9 .&-]{3,90}?)\s*,?\s+(?:expressed\s+(?:its|their)\s+intention\s+to|decided\s+to|has)\s+withdraw\b", flat, re.I)
        if m: name("withdrawn_submitters", m.group(1), n, m.group(0))
        m = re.search(r"\b(?:Resolution\s+Applicant|SRA)\s*,?\s+([A-Z][A-Za-z0-9 .&-]+?(?:Private\s+Limited|Pvt\.?\s*Ltd\.?|Limited))\s+incorporated\s+on\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})", flat, re.I)
        if m:
            add("applicant_incorporated", iso_date(m.group(2)), n, m.group(0))
            summary = flat[m.end():m.end()+260]
            profile = re.search(r"\bis\s+a\s+(.{8,130}?)\s+based\s+in\s+([A-Z][A-Za-z ]{3,60}?),\s*([A-Z][A-Za-z ]{3,40})", summary, re.I)
            if profile:
                add("applicant_business", profile.group(1), n, profile.group(0))
                add("applicant_location", f"{profile.group(2)}, {profile.group(3)}", n, profile.group(0))
        m = re.search(r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\s*,?\s*Director\s+of\s+(?:the\s+)?(?:SRA|Successful\s+Resolution\s+Applicant|Resolution\s+Applicant)\b", flat, re.I)
        if m: name("applicant_directors", m.group(1), n, m.group(0))
        if "Fair Value" in flat and "Liquidation Value" in flat:
            sections = re.split(r"\b(?=[1-9]\s+(?:Land\s+and\s+Building|Plant\s*(?:&|and)\s*Machinery|Security\s+and\s+Financial\s+Assets))", flat, flags=re.I)
            for section in sections:
                heading = re.match(r"\d+\s+(Land\s+and\s+Building|Plant\s*(?:&|and)\s*Machinery|Security\s+and\s+Financial\s+Assets)", section, re.I)
                if not heading: continue
                asset = " ".join(heading.group(1).split())
                average = re.search(
                    r"\bAverage(?:\s*\(?(?:L&B|P&M|S&FA)\)?)?\s+"
                    r"([\d,]+)(?:/\s*-)?\s+([\d,]+)(?:/\s*-)?",
                    section, re.I,
                )
                if average:
                    basis, values, excerpt = "Average", average.groups(), average.group(0)
                elif not re.search(r"\bValuer\s*\d+\b", section, re.I):
                    # A single directly reported value needs no averaging.
                    reported = re.search(
                        r"([\d,]+)(?:/\s*-)?\s+([\d,]+)(?:/\s*-)?",
                        section[heading.end():],
                    )
                    if not reported:
                        continue
                    basis, values, excerpt = "Reported", reported.groups(), reported.group(0)
                else:
                    # The PDF lists valuers but no supported average.
                    continue
                if any(v["asset"].casefold() == asset.casefold()
                       for v in record["asset_values"]):
                    continue
                fair, liquidation = (money(value) for value in values)
                if fair is None or liquidation is None:
                    continue
                record["asset_values"].append({
                    "asset": asset, "basis": basis, "fair_value": fair,
                    "liquidation_value": liquidation, "document_id": document_id,
                    "page": n,
                })
                evidence.append({
                    "field": "asset_values", "page": n, "excerpt": excerpt,
                })
        if re.search(r"summary\s+of\s+the\s+payments\s+to\s+be\s+made\s+under\s+the\s+Resolution\s+Plan", flat, re.I):
            table = re.split(r"\bTotal\s+Plan\s+Value\b", flat.split("Sr. No.", 1)[-1], maxsplit=1, flags=re.I)[0]
            for row in re.finditer(r"(\d{1,2})\.\s+(.+?)(?=\s+\d{1,2}\.\s+|$)", table, re.I):
                block = row.group(2)
                tokens = list(AMOUNT_TOKEN.finditer(block))
                if not tokens: continue
                recipient = " ".join(block[:tokens[0].start()].strip(" -").split())
                if not 3 <= len(recipient) <= 180: continue
                amounts = [money(token.group(0).replace("/-", "")) for token in tokens]
                if any(x is None for x in amounts): continue
                amount, admitted = amounts[-1], amounts[-2] if len(amounts) > 1 else None
                if any(x["recipient"].casefold() == recipient.casefold() for x in record["plan_payments"]): continue
                record["plan_payments"].append({"recipient": recipient, "amount": amount, "admitted": admitted, "document_id": document_id, "page": n})
                evidence.append({"field": "plan_payments", "page": n, "excerpt": row.group(0)[:1000]})
                if "CIRP Cost" in recipient or "EPFO" in recipient or admitted is None: continue
                matching = [c for c in claim_rows if c["category"] == category(recipient) and c.get("scope") == "category_total" and c.get("plan_amount") is None and c.get("admitted") == admitted]
                if len(matching) == 1:
                    matching[0]["plan_amount"] = amount
                    matching[0]["notes"] += f" Proposed payment from plan table on PDF page {n}."
