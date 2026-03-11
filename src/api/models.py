"""
Pydantic models for API request/response schemas.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────

class Decision(str, Enum):
    APPROVE = "APPROVE"
    CONDITIONAL_APPROVE = "CONDITIONAL_APPROVE"
    REFER_TO_HUMAN = "REFER_TO_HUMAN"
    DECLINE = "DECLINE"


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ── Nested models ────────────────────────────────────────────────────

class Property(BaseModel):
    address: str
    type: str
    appraised_value: float
    purchase_price: float


class Loan(BaseModel):
    requested_amount: float
    term_months: int
    interest_rate_requested: float


class Borrower(BaseModel):
    first_name: str
    last_name: str
    ssn_last4: str
    dob: str
    email: str
    phone: str
    citizenship: str


class Employment(BaseModel):
    employer_name: str
    title: str
    years_at_job: float
    monthly_gross_income: float


class Income(BaseModel):
    monthly_gross: float
    other_monthly_income: float = 0


class Debts(BaseModel):
    monthly_auto_loan: float = 0
    monthly_student_loan: float = 0
    monthly_credit_card_min: float = 0
    monthly_other: float = 0


class Assets(BaseModel):
    checking_balance: float = 0
    savings_balance: float = 0
    retirement_balance: float = 0


class Credit(BaseModel):
    fico_score: int
    derogatory_marks: int = 0
    bankruptcies: int = 0
    foreclosures: int = 0


class Declarations(BaseModel):
    is_primary_residence: bool = True
    has_outstanding_judgments: bool = False
    is_party_to_lawsuit: bool = False
    has_prior_foreclosure: bool = False


# ── Application ──────────────────────────────────────────────────────

class ApplicationPayload(BaseModel):
    application_id: str
    submission_date: str
    loan_type: str
    loan_purpose: str
    property: Property
    loan: Loan
    borrower: Borrower
    employment: Employment
    income: Income
    debts: Debts
    assets: Assets
    credit: Credit
    declarations: Declarations


# ── Metrics ──────────────────────────────────────────────────────────

class Metrics(BaseModel):
    dti: Optional[float] = None
    ltv: Optional[float] = None
    monthly_income: Optional[float] = None
    monthly_debt: Optional[float] = None
    residual_income: Optional[float] = None


# ── Workflow result ──────────────────────────────────────────────────

class WorkflowResult(BaseModel):
    application_id: str
    decision: Decision
    human_review_required: bool = False
    reasons: list[str] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)
    risk_flags: list[str] = Field(default_factory=list)
    compliance_notes: list[str] = Field(default_factory=list)
    borrower_message: str = ""
    underwriter_summary: str = ""


# ── Status response ─────────────────────────────────────────────────

class ApplicationStatus(BaseModel):
    application_id: str
    status: RunStatus
    result: Optional[WorkflowResult] = None
