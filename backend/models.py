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


class CaseRecord(StrictModel):
    # Every populated identity/ownership field carries its own source reference.
    company: Fact | None = None
    cin: Fact | None = None
    case_number: Fact | None = None
    tribunal: Fact | None = None
    admission_date: DateFact | None = None
    resolution_date: DateFact | None = None
    order_date: Fact | None = None
    irp: Fact | None = None
    professional: Fact | None = None
    liquidator: Fact | None = None
    applicant: Fact | None = None
    previous_owners: list[Fact] = Field(default_factory=list)
    current_owners: list[Fact] = Field(default_factory=list)
    subsequent_owners: list[Fact] = Field(default_factory=list)
    previous_directors: list[Fact] = Field(default_factory=list)
    current_directors: list[Fact] = Field(default_factory=list)
    subsequent_directors: list[Fact] = Field(default_factory=list)
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


def metrics(record: CaseRecord) -> dict:
    # Matched rows only: unknown recovery is not zero. Actual payments are separate.
    eligible = [c for c in record.claims if c.admitted is not None and c.admitted > 0 and c.plan_amount is not None]
    admitted = sum((c.admitted for c in eligible), Decimal(0))
    planned = sum((c.plan_amount for c in eligible), Decimal(0))
    haircut = ((admitted - planned) / admitted * 100).quantize(Decimal("0.01")) if admitted else None
    return {"matched_admitted_inr": str(admitted), "matched_plan_inr": str(planned),
            "plan_haircut_percent": str(haircut) if haircut is not None else None,
            "matched_rows": len(eligible), "total_rows": len(record.claims)}
