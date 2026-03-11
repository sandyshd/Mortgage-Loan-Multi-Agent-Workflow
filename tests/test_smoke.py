"""
Smoke tests for the Mortgage Loan Origination API.

Runs a few sample applications through the API and asserts the response
matches the expected JSON schema. Does NOT require a live Azure AI Foundry
connection – it tests the API schema and validation layer only.

Usage:
    pytest tests/test_smoke.py -v
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models import Decision, RunStatus

client = TestClient(app)

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


def _load_sample(name: str) -> dict:
    return json.loads((SAMPLES_DIR / name).read_text())


# ── Health check ─────────────────────────────────────────────────────

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["demo"] is True


# ── Submit application ───────────────────────────────────────────────

def test_submit_application_1():
    payload = _load_sample("application_1.json")
    resp = client.post("/applications", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["application_id"] == "APP-2026-00101"
    assert data["status"] == RunStatus.PENDING.value


def test_resubmit_application_resets():
    payload = _load_sample("application_1.json")
    # First submit
    client.post("/applications", json=payload)
    # Re-submit should succeed and reset to PENDING
    resp = client.post("/applications", json=payload)
    assert resp.status_code == 201
    assert resp.json()["status"] == RunStatus.PENDING.value


def test_submit_application_2():
    payload = _load_sample("application_2.json")
    resp = client.post("/applications", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["application_id"] == "APP-2026-00102"


# ── Status check ─────────────────────────────────────────────────────

def test_get_status_existing():
    payload = _load_sample("application_1.json")
    client.post("/applications", json=payload)
    resp = client.get(f"/applications/{payload['application_id']}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["application_id"] == payload["application_id"]
    assert data["status"] in [s.value for s in RunStatus]


def test_get_status_not_found():
    resp = client.get("/applications/NONEXISTENT/status")
    assert resp.status_code == 404


# ── Schema validation ───────────────────────────────────────────────

def test_workflow_result_schema():
    """Ensure the WorkflowResult model accepts all valid decision types."""
    from src.api.models import WorkflowResult, Metrics

    for decision in Decision:
        result = WorkflowResult(
            application_id="TEST-001",
            decision=decision,
            human_review_required=decision in (Decision.REFER_TO_HUMAN, Decision.DECLINE),
            reasons=["Test reason"],
            missing_documents=[],
            metrics=Metrics(dti=0.35, ltv=0.80, monthly_income=9500, monthly_debt=920),
            risk_flags=[],
            compliance_notes=[],
            borrower_message="Test message",
            underwriter_summary="Test summary",
        )
        assert result.decision == decision
        data = result.model_dump()
        assert "application_id" in data
        assert "decision" in data
        assert "metrics" in data
        assert isinstance(data["reasons"], list)


def test_application_payload_validation():
    """Ensure the application payload schema validates correctly."""
    payload = _load_sample("application_1.json")
    from src.api.models import ApplicationPayload

    app_model = ApplicationPayload(**payload)
    assert app_model.application_id == payload["application_id"]
    assert app_model.borrower.first_name == "Jane"
    assert app_model.credit.fico_score == 742


def test_application_2_payload_validation():
    payload = _load_sample("application_2.json")
    from src.api.models import ApplicationPayload

    app_model = ApplicationPayload(**payload)
    assert app_model.loan_type == "FHA"
    assert app_model.credit.fico_score == 648
