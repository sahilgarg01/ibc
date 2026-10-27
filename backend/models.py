from decimal import Decimal
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Fact(StrictModel):
    value: str = Field(min_length=1, max_length=500)
    document_id: str
    page: int = Field(ge=1)
    din: str | None = Field(default=None, pattern=r"^\d{8}$")
    cin: str | None = Field(default=None, pattern=r"^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$")


class DateFact(Fact):
    @model_validator(mode="after")
    def valid_date(self):
        date.fromisoformat(self.value)
        return self


class Plan(StrictModel):
    applicant: str = Field(min_length=1, max_length=500)
    amount: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    admitted: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=2)
    document_id: str
    page: int = Field(ge=1)


Money = Decimal


class Claim(StrictModel):
    creditor: str = Field(min_length=1, max_length=300)
    category: Literal["secured_financial", "unsecured_financial", "operational", "employee", "government", "other"]
    scope: Literal["individual", "category_total"] = "individual"
    claimed: Money | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    admitted: Money | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    plan_amount: Money | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    actual_paid: Money | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    document_id: str
    page: int = Field(ge=1)
    notes: str = Field(default="", max_length=2000)


class AssetValue(StrictModel):
    asset: str = Field(min_length=2, max_length=150)
    basis: str = Field(min_length=2, max_length=100)
    fair_value: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    liquidation_value: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    document_id: str
    page: int = Field(ge=1)
    fair_value_page: int | None = Field(default=None, ge=1)
    liquidation_value_page: int | None = Field(default=None, ge=1)
    fair_value_source_unit: Literal['INR', 'crore', 'lakh', 'million'] = 'INR'
    liquidation_value_source_unit: Literal['INR', 'crore', 'lakh', 'million'] = 'INR'


class PlanPayment(StrictModel):
    recipient: str = Field(min_length=2, max_length=200)
    amount: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    admitted: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    document_id: str
    page: int = Field(ge=1)


class CaseRecord(StrictModel):
    # Every populated identity/ownership field carries its own source reference.
    company: Fact | None = None
    cin: Fact | None = None
    registered_office: Fact | None = None
    industry: Fact | None = None
    case_number: Fact | None = None
    tribunal: Fact | None = None
    admission_date: DateFact | None = None
    resolution_date: DateFact | None = None
    order_date: Fact | None = None
    irp: Fact | None = None
    professional: Fact | None = None
    liquidator: Fact | None = None
    applicant: Fact | None = None
    applicant_cin: Fact | None = None
    fair_value: Fact | None = None
    liquidation_value: Fact | None = None
    total_received_claims: Fact | None = None
    total_admitted_claims: Fact | None = None
    coc_voting_share: Fact | None = None
    applicant_incorporated: Fact | None = None
    applicant_business: Fact | None = None
    applicant_location: Fact | None = None
    applicant_directors: list[Fact] = Field(default_factory=list)
    judges: list[Fact] = Field(default_factory=list)
    withdrawn_submitters: list[Fact] = Field(default_factory=list)
    asset_values: list[AssetValue] = Field(default_factory=list)
    plan_payments: list[PlanPayment] = Field(default_factory=list)
    financial_creditors: list[Fact] = Field(default_factory=list)
    plan_submitters: list[Fact] = Field(default_factory=list)
    previous_owners: list[Fact] = Field(default_factory=list)
    current_owners: list[Fact] = Field(default_factory=list)
    subsequent_owners: list[Fact] = Field(default_factory=list)
    previous_directors: list[Fact] = Field(default_factory=list)
    current_directors: list[Fact] = Field(default_factory=list)
    subsequent_directors: list[Fact] = Field(default_factory=list)
    proposed_owners: list[Fact] = Field(default_factory=list)
    proposed_directors: list[Fact] = Field(default_factory=list)
    outcome: Literal["unknown", "ongoing", "resolution_approved", "liquidation_ordered", "liquidation_completed"] = "unknown"
    outcome_evidence: Fact | None = None
    claims: list[Claim] = Field(default_factory=list, max_length=500)
    plans: list[Plan] = Field(default_factory=list, max_length=100)
    notes: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def avoid_double_counting(self):
        if self.admission_date and self.resolution_date and self.resolution_date.value < self.admission_date.value:
            raise ValueError("Resolution date cannot precede admission date")
        if self.cin:
            import re
            if not re.fullmatch(r"[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}", self.cin.value):
                raise ValueError("Debtor CIN must be a valid 21-character CIN")
        for category in {c.category for c in self.claims}:
            rows = [c for c in self.claims if c.category == category]
            if any(c.scope == "category_total" for c in rows) and len(rows) > 1:
                raise ValueError(f"Use either one category total or individual creditors for {category}, not both")
        keys = [(c.category, c.creditor.casefold()) for c in self.claims]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate creditor in the same category")
        return self


class SaveRecord(StrictModel):
    version: int = Field(ge=1)
    record: CaseRecord


class Approval(StrictModel):
    version: int = Field(ge=1)
    reviewer: str = Field(min_length=2, max_length=100)
    confirmed: bool


def preferred_asset_values(values):
    """Prefer an average, otherwise average multiple rows or retain one row per asset."""
    grouped = {}
    for value in values:
        key = " ".join(value.asset.casefold().replace("&", "and").split())
        grouped.setdefault(key, []).append(value)
    chosen = []
    for rows in grouped.values():
        average = next((row for row in rows if row.basis.casefold() == "average"), None)
        calculated = next((row for row in rows if row.basis.casefold() == "calculated average"), None)
        if average or calculated:
            chosen.append(average or calculated)
        elif len(rows) > 1:
            def mean(field):
                amounts = [getattr(row, field) for row in rows if getattr(row, field) is not None]
                return (sum(amounts, Decimal(0)) / len(amounts)).quantize(Decimal('0.01')) if amounts else None
            chosen.append(rows[0].model_copy(update={
                'basis': 'Calculated average',
                'fair_value': mean('fair_value'),
                'liquidation_value': mean('liquidation_value'),
            }))
        else:
            chosen.append(rows[0])
    return chosen


def metrics(record: CaseRecord) -> dict:
    # Matched rows only: unknown recovery is not zero. Actual payments are separate.
    eligible = [c for c in record.claims if c.admitted is not None and c.admitted > 0 and c.plan_amount is not None]
    admitted = sum((c.admitted for c in eligible), Decimal(0))
    planned = sum((c.plan_amount for c in eligible), Decimal(0))
    basis = 'matched_claims' if admitted else None
    if not admitted and len(record.plans)==1:
        plan=record.plans[0]
        if plan.admitted is not None and plan.amount is not None:
            admitted,planned=plan.admitted,plan.amount
            basis='plan_total'
    haircut = ((admitted - planned) / admitted * 100).quantize(Decimal("0.01")) if admitted else None
    asset_values = preferred_asset_values(record.asset_values)
    fair_values = [item.fair_value for item in asset_values]
    liquidation_values = [item.liquidation_value for item in asset_values]
    fair_breakdown = (sum(fair_values, Decimal(0))
                      if fair_values and all(value is not None for value in fair_values) else None)
    liquidation_breakdown = (sum(liquidation_values, Decimal(0))
                             if liquidation_values and all(value is not None for value in liquidation_values)
                             else None)
    def stated_total(fact):
        try:
            return Decimal(fact.value) if fact else None
        except (ValueError, ArithmeticError):
            return None
    fair_total = stated_total(record.fair_value)
    liquidation_total = stated_total(record.liquidation_value)
    # Category totals and named creditors are mutually exclusive per category.
    received_values = [item.claimed for item in record.claims]
    admitted_values = [item.admitted for item in record.claims]
    received_breakdown = (sum(received_values, Decimal(0))
                          if received_values and all(value is not None for value in received_values)
                          else None)
    admitted_breakdown = (sum(admitted_values, Decimal(0))
                          if admitted_values and all(value is not None for value in admitted_values)
                          else None)
    received_total = stated_total(record.total_received_claims)
    admitted_total = stated_total(record.total_admitted_claims)
    payment_sum = sum((item.amount for item in record.plan_payments), Decimal(0))
    plan_total = record.plans[0].amount if len(record.plans) == 1 else None
    return {"matched_admitted_inr": str(admitted), "matched_plan_inr": str(planned),
            "plan_haircut_percent": str(haircut) if haircut is not None else None,
            "matched_rows": len(eligible), "total_rows": len(record.claims), "basis": basis,
            "claim_received_breakdown_inr": str(received_breakdown) if received_breakdown is not None else None,
            "claim_admitted_breakdown_inr": str(admitted_breakdown) if admitted_breakdown is not None else None,
            "claim_received_matches_total": received_breakdown == received_total if received_breakdown is not None and received_total is not None else None,
            "claim_admitted_matches_total": admitted_breakdown == admitted_total if admitted_breakdown is not None and admitted_total is not None else None,
            "asset_valuations": [item.model_dump(mode="json") for item in asset_values],
            "asset_fair_breakdown_inr": str(fair_breakdown) if fair_breakdown is not None else None,
            "asset_liquidation_breakdown_inr": str(liquidation_breakdown) if liquidation_breakdown is not None else None,
            "asset_fair_matches_total": fair_breakdown == fair_total if fair_breakdown is not None and fair_total is not None else None,
            "asset_liquidation_matches_total": liquidation_breakdown == liquidation_total if liquidation_breakdown is not None and liquidation_total is not None else None,
            "plan_payments_sum_inr": str(payment_sum) if record.plan_payments else None,
            "plan_payments_match_total": payment_sum == plan_total if record.plan_payments and plan_total is not None else None}
