"""
FastAPI service for the Mortgage Loan Origination multi-agent workflow.

Endpoints:
    POST /applications            – Submit a new application
    POST /applications/{id}/run   – Trigger the workflow
    GET  /applications/{id}/status – Check status / retrieve result
"""

from __future__ import annotations

import json
import os
import uuid
import logging
from pathlib import Path
from typing import Any

import requests as http_requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from azure.identity import DefaultAzureCredential

from .models import (
    ApplicationPayload,
    ApplicationStatus,
    Decision,
    Metrics,
    RunStatus,
    WorkflowResult,
)

# ── Configuration ────────────────────────────────────────────────────
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

ENDPOINT = os.environ.get("PROJECT_ENDPOINT", "").rstrip("/")
MODEL = os.environ.get("MODEL_DEPLOYMENT", "gpt-4o")
API_VERSION = "2025-05-15-preview"
AGENT_IDS_PATH = Path(__file__).resolve().parents[1] / "foundry" / "agent_ids.json"

logger = logging.getLogger("mortgage_api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Mortgage Loan Origination – AI Foundry Demo",
    version="1.0.0",
    description=(
        "Multi-agent workflow demo for mortgage loan origination. "
        "**DISCLAIMER: Synthetic demo only — not real underwriting.**"
    ),
)

# ── CORS ─────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static frontend ─────────────────────────────────────────────────
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@app.get("/", include_in_schema=False)
def serve_frontend():
    return FileResponse(FRONTEND_DIR / "index.html")

# ── In-memory store (demo only – use a real DB in production) ────────
_applications: dict[str, dict[str, Any]] = {}


# ── Helpers ──────────────────────────────────────────────────────────

def _load_agent_ids() -> dict[str, str]:
    if not AGENT_IDS_PATH.exists():
        raise RuntimeError(
            f"Agent IDs file not found at {AGENT_IDS_PATH}. "
            "Run `python src/foundry/create_agents.py` first."
        )
    return json.loads(AGENT_IDS_PATH.read_text())


def _load_docs_for_app(app_id: str) -> list[dict]:
    meta_path = Path(__file__).resolve().parents[2] / "data" / "samples" / "docs_metadata.json"
    if not meta_path.exists():
        return []
    meta = json.loads(meta_path.read_text())
    if meta.get("application_id") == app_id:
        docs = meta.get("documents", [])
    else:
        docs = meta.get("_app2_documents", meta.get("documents", []))

    # Enrich with text snippets
    texts_dir = Path(__file__).resolve().parents[2] / "data" / "samples" / "sample_doc_texts"
    if texts_dir.exists():
        texts = {f.name: f.read_text() for f in texts_dir.iterdir() if f.is_file()}
        for doc in docs:
            fname = doc.get("filename")
            if fname and fname in texts:
                doc["text_snippet"] = texts[fname]
    return docs


_DECISION_ALIASES: dict[str, str] = {
    "REFER": "REFER_TO_HUMAN",
    "REFER_TO_HUMAN": "REFER_TO_HUMAN",
    "CONDITIONAL": "CONDITIONAL_APPROVE",
    "CONDITIONALLY_APPROVE": "CONDITIONAL_APPROVE",
    "CONDITIONALLY_APPROVED": "CONDITIONAL_APPROVE",
    "APPROVED": "APPROVE",
    "DECLINED": "DECLINE",
    "DENIED": "DECLINE",
    "DENY": "DECLINE",
}


def _normalise_decision(raw_decision: str) -> Decision:
    upper = raw_decision.strip().upper()
    mapped = _DECISION_ALIASES.get(upper, upper)
    try:
        return Decision(mapped)
    except ValueError:
        logger.warning("Unknown decision value '%s', defaulting to REFER_TO_HUMAN", raw_decision)
        return Decision.REFER_TO_HUMAN


def _parse_result(raw: str, app_id: str) -> WorkflowResult:
    """Best-effort parse of the orchestrator's JSON response."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return WorkflowResult(
            application_id=app_id,
            decision=Decision.REFER_TO_HUMAN,
            human_review_required=True,
            reasons=["Unable to parse agent response"],
            borrower_message="We were unable to process your application automatically. A human underwriter will review it.",
            underwriter_summary=raw[:2000],
        )

    metrics_raw = data.get("metrics", {})
    return WorkflowResult(
        application_id=data.get("application_id", app_id),
        decision=_normalise_decision(data.get("decision", "REFER_TO_HUMAN")),
        human_review_required=data.get("human_review_required", False),
        reasons=data.get("reasons", []),
        missing_documents=data.get("missing_documents", []),
        metrics=Metrics(
            dti=metrics_raw.get("dti"),
            ltv=metrics_raw.get("ltv"),
            monthly_income=metrics_raw.get("monthly_income"),
            monthly_debt=metrics_raw.get("monthly_debt"),
            residual_income=metrics_raw.get("residual_income"),
        ),
        risk_flags=data.get("risk_flags", []),
        compliance_notes=data.get("compliance_notes", []),
        borrower_message=data.get("borrower_message", ""),
        underwriter_summary=data.get("underwriter_summary", ""),
    )


# ── Background task ─────────────────────────────────────────────────

_credential = DefaultAzureCredential()


def _get_headers() -> dict:
    token = _credential.get_token("https://ai.azure.com/.default").token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _call_agent(agent_name: str, message: str, timeout: int = 120, is_workflow: bool = False) -> str:
    """Send *message* to a Foundry agent via the Responses API and return the text reply.

    For workflow agents, a conversation is created first (required by the API).
    """
    headers = _get_headers()
    payload: dict[str, Any] = {
        "model": MODEL,
        "input": [{"role": "user", "content": message}],
        "agent_reference": {
            "type": "agent_reference",
            "name": agent_name,
            "version": "1",
        },
    }

    # Workflow agents require a conversation
    if is_workflow:
        conv_url = f"{ENDPOINT}/conversations?api-version={API_VERSION}"
        cr = http_requests.post(conv_url, headers=headers, json={}, timeout=30)
        cr.raise_for_status()
        conv_id = cr.json()["id"]
        payload["conversation"] = {"id": conv_id}
        logger.info("Created conversation %s for workflow %s", conv_id, agent_name)

    url = f"{ENDPOINT}/openai/responses?api-version={API_VERSION}"
    r = http_requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code != 200:
        logger.error("Agent %s call failed: %s %s — %s", agent_name, r.status_code, r.reason, r.text[:1000])
    r.raise_for_status()
    data = r.json()
    # Collect all text outputs (workflow returns workflow_action and message items)
    texts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    texts.append(c.get("text", ""))
        elif item.get("type") == "workflow_action":
            # Workflow actions may contain nested output
            action_output = item.get("output", "")
            if action_output:
                texts.append(action_output if isinstance(action_output, str) else json.dumps(action_output))

    if is_workflow:
        # For workflows, return ALL agent texts so the caller can merge JSON
        # blocks from every agent in the chain (intake → comms).
        return "\n\n".join(texts) if texts else "{}"
    return texts[-1] if texts else "{}"


def _extract_workflow_result(raw_response: str, app_id: str) -> dict:
    """Extract the consolidated result from the workflow's final output.

    The workflow chains 7 agents. The last two (OrchestratorAgent → CommsAgent)
    produce the decision and borrower/underwriter messages. We attempt to
    merge them from the raw response.
    """
    # Try to parse as JSON directly (CommsAgent output)
    try:
        data = json.loads(raw_response)
        # If it has decision fields, it's already the consolidated result
        if "decision" in data or "borrower_message" in data:
            return data
    except json.JSONDecodeError:
        pass

    # Try to find JSON blocks in the response text
    json_blocks = []
    brace_depth = 0
    start = -1
    for i, ch in enumerate(raw_response):
        if ch == '{':
            if brace_depth == 0:
                start = i
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0 and start >= 0:
                try:
                    block = json.loads(raw_response[start:i + 1])
                    json_blocks.append(block)
                except json.JSONDecodeError:
                    pass
                start = -1

    if not json_blocks:
        return {"decision": "REFER_TO_HUMAN", "human_review_required": True,
                "reasons": ["Could not parse workflow output"], "underwriter_summary": raw_response[:2000]}

    # Merge all JSON blocks — later blocks override earlier ones
    merged = {}
    for block in json_blocks:
        merged.update(block)
    return merged


def _run_workflow(app_id: str):
    """Execute the multi-agent workflow by invoking the MortgageLoanOrigination
    workflow agent. The workflow chains all 7 specialist agents via
    InvokeAzureAgent steps and returns the final CommsAgent output.
    """
    try:
        _applications[app_id]["status"] = RunStatus.RUNNING
        agent_names = _load_agent_ids()
        application = _applications[app_id]["payload"]
        docs = _load_docs_for_app(app_id)

        payload_json = json.dumps({"application": application, "documents": docs}, indent=2)

        workflow_name = agent_names.get("workflow_name", "MortgageLoanOrigination")
        logger.info("[%s] Invoking workflow %s …", app_id, workflow_name)

        raw_response = _call_agent(workflow_name, payload_json, timeout=600, is_workflow=True)
        logger.info("[%s] Workflow raw response length: %d chars", app_id, len(raw_response))

        # The workflow's last agent (CommsAgent) produces the final output.
        # Try to parse the last meaningful JSON from the response.
        result_data = _extract_workflow_result(raw_response, app_id)
        logger.info("[%s] Extracted result keys: %s", app_id, list(result_data.keys()))
        result_data.setdefault("application_id", app_id)

        result = _parse_result(json.dumps(result_data), app_id)
        _applications[app_id]["status"] = RunStatus.COMPLETED
        _applications[app_id]["result"] = result

    except Exception as exc:
        logger.exception("Workflow failed for %s", app_id)
        _applications[app_id]["status"] = RunStatus.FAILED
        _applications[app_id]["result"] = WorkflowResult(
            application_id=app_id,
            decision=Decision.REFER_TO_HUMAN,
            human_review_required=True,
            reasons=[f"Workflow error: {exc}"],
        )


# ── Endpoints ────────────────────────────────────────────────────────

@app.post("/applications", response_model=ApplicationStatus, status_code=201)
def submit_application(payload: ApplicationPayload):
    """Submit a new mortgage application (re-submit overwrites previous)."""
    app_id = payload.application_id

    _applications[app_id] = {
        "payload": payload.model_dump(),
        "status": RunStatus.PENDING,
        "result": None,
    }
    return ApplicationStatus(application_id=app_id, status=RunStatus.PENDING)


@app.post("/applications/{app_id}/run", response_model=ApplicationStatus)
def run_workflow(app_id: str, background_tasks: BackgroundTasks):
    """Trigger the multi-agent workflow for a submitted application."""
    if app_id not in _applications:
        raise HTTPException(status_code=404, detail="Application not found")
    if _applications[app_id]["status"] == RunStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail="Application is already running",
        )

    _applications[app_id]["status"] = RunStatus.RUNNING
    background_tasks.add_task(_run_workflow, app_id)
    return ApplicationStatus(application_id=app_id, status=RunStatus.RUNNING)


@app.get("/applications/{app_id}/status", response_model=ApplicationStatus)
def get_status(app_id: str):
    """Check status and retrieve the workflow result."""
    if app_id not in _applications:
        raise HTTPException(status_code=404, detail="Application not found")
    entry = _applications[app_id]
    return ApplicationStatus(
        application_id=app_id,
        status=entry["status"],
        result=entry.get("result"),
    )


@app.get("/health")
def health():
    return {"status": "ok", "demo": True, "disclaimer": "Synthetic demo – not real underwriting"}
