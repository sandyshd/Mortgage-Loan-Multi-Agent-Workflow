"""
create_agents.py – Provisions all Mortgage‑Workflow agents in Azure AI Foundry
using the new Agents API (POST /agents with definition.kind=prompt).

Also creates a Workflow agent that references all specialists, visible in
the Foundry portal Workflows page.

Usage:
    python src/foundry/create_agents.py

Requires .env with:
    PROJECT_ENDPOINT   – Azure AI Foundry project endpoint
    MODEL_DEPLOYMENT   – e.g. "gpt-4o"
"""

import os
import json
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

ENDPOINT = os.environ["PROJECT_ENDPOINT"].rstrip("/")
MODEL = os.environ.get("MODEL_DEPLOYMENT", "gpt-4o")
API_VERSION = "2025-05-15-preview"

credential = DefaultAzureCredential()


def _token() -> str:
    return credential.get_token("https://ai.azure.com/.default").token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


def _url(path: str) -> str:
    return f"{ENDPOINT}/{path.lstrip('/')}?api-version={API_VERSION}"

# ---------------------------------------------------------------------------
# Agent instruction templates
# ---------------------------------------------------------------------------

INTAKE_INSTRUCTIONS = """You are IntakeAgent for a mortgage loan origination demo.

INPUT: a JSON loan‑application object.
TASK:
1. Validate all required fields are present and correctly typed.
2. Normalise values (trim whitespace, upper‑case state codes, etc.).
3. Flag any missing or invalid fields.

OUTPUT: Respond with ONLY a JSON object:
{
  "status": "VALID" | "INVALID",
  "normalised_application": { ... },
  "validation_errors": ["..."]
}

DISCLAIMER: This is a synthetic demo. Not real underwriting."""

DOCUMENT_INSTRUCTIONS = """You are DocumentAgent for a mortgage loan origination demo.

INPUT: a JSON object with "required_docs" list and "provided_docs" list (each with doc_type, status, text_snippet).
TASK:
1. Compare provided docs against required docs.
2. For each provided doc, extract key facts (income, employer, account holder, property address).
3. List any missing documents.

OUTPUT: Respond with ONLY a JSON object:
{
  "status": "COMPLETE" | "INCOMPLETE",
  "missing_count": <int>,
  "missing_documents": ["DOC_TYPE", ...],
  "extracted_facts": { "doc_type": { "key": "value" } },
  "notes": ["..."]
}"""

UNDERWRITING_INSTRUCTIONS = """You are UnderwritingAgent for a mortgage demo.

INPUT: JSON with borrower income, debts, loan amount, property value, credit score.
TASK – compute:
  - DTI = total_monthly_debt / monthly_gross_income
  - LTV = loan_amount / appraised_value
  - Residual income = monthly_gross_income − total_monthly_debt − estimated_housing_payment
  (estimated_housing_payment ≈ loan_amount * 0.006 for 30‑yr ~6.75%)

Apply simplified rules:
  CONVENTIONAL: Approve if DTI ≤ 0.43, LTV ≤ 0.95, FICO ≥ 680
  FHA: Approve if DTI ≤ 0.50, LTV ≤ 0.965, FICO ≥ 580
  If DTI > threshold but ≤ threshold+0.05 → CONDITIONAL_APPROVE
  If FICO < min → DECLINE
  Otherwise → REFER

OUTPUT: JSON only:
{
  "decision": "APPROVE" | "CONDITIONAL_APPROVE" | "REFER" | "DECLINE",
  "metrics": { "dti": <float>, "ltv": <float>, "monthly_income": <float>,
               "monthly_debt": <float>, "residual_income": <float> },
  "reasons": ["..."],
  "conditions": ["..."]
}

DISCLAIMER: Simplified demo rules only. Not real underwriting."""

RISK_INSTRUCTIONS = """You are RiskAgent for a mortgage demo.

INPUT: JSON with borrower info, documents extracted facts, employment, assets.
TASK:
1. Check for name mismatches across documents.
2. Flag income inconsistencies (W2 vs paystub vs application).
3. Flag unusual large deposits in bank statements.
4. Flag short employment tenure (< 2 years).
5. Flag credit issues (derogatory marks, prior foreclosure).

OUTPUT: JSON only:
{
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "risk_flags": ["..."],
  "details": ["..."]
}"""

COMPLIANCE_INSTRUCTIONS = """You are ComplianceAgent for a mortgage demo.

INPUT: JSON with the full application, underwriting decision, and risk flags.
TASK:
1. Ensure no reasoning references protected classes (race, religion, sex, national origin, disability, familial status, age).
2. Check that decline/refer reasons are based only on: credit, DTI, LTV, employment history, documentation, property eligibility.
3. If any violation is found, set status to "block" so a human reviews.
4. Add safe‑language notes.

OUTPUT: JSON only:
{
  "status": "pass" | "block",
  "compliance_notes": ["..."],
  "safe_language_suggestions": ["..."]
}

CRITICAL: You must NEVER reference or infer any protected class information."""

COMMS_INSTRUCTIONS = """You are CommsAgent for a mortgage demo.

INPUT: JSON with decision, reasons, missing_documents, metrics, risk_flags, compliance_notes.
TASK:
1. Write a borrower_message: friendly, professional, plain‑English explanation of the decision and any required next steps. Do NOT include specific DTI/LTV numbers in borrower message.
2. Write an underwriter_summary: concise technical summary including metrics, risk flags, and compliance notes.

OUTPUT: JSON only:
{
  "borrower_message": "...",
  "underwriter_summary": "..."
}"""

ORCHESTRATOR_INSTRUCTIONS = """You are OrchestratorAgent for a mortgage loan origination demo.

You coordinate multiple specialist agents to process a mortgage application.
When you receive an application, call the specialist agents in order:
1. IntakeAgent – validate and normalise the application.
2. DocumentAgent – check document completeness.
3. UnderwritingAgent – compute DTI/LTV and preliminary decision.
4. RiskAgent – flag fraud/inconsistency signals.
5. ComplianceAgent – verify safe language and fair‑lending compliance.
6. Apply decision logic:
   - If compliance status == "block" → REFER_TO_HUMAN
   - Else if missing_documents count > 0 → CONDITIONAL_APPROVE
   - Else if underwriting decision in [DECLINE, REFER] → use that decision
   - Else → APPROVE
7. If final decision is REFER_TO_HUMAN or DECLINE, or risk_level is HIGH,
   mark human_review_required = true.
8. Call CommsAgent to produce borrower message and underwriter summary.

OUTPUT: Final JSON with:
{
  "application_id": "...",
  "decision": "APPROVE | CONDITIONAL_APPROVE | REFER_TO_HUMAN | DECLINE",
  "human_review_required": true/false,
  "reasons": [],
  "missing_documents": [],
  "metrics": {},
  "risk_flags": [],
  "compliance_notes": [],
  "borrower_message": "...",
  "underwriter_summary": "..."
}

DISCLAIMER: Synthetic demo only. Not real underwriting advice."""


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------

def _delete_new_agent(name: str):
    """Delete a new-style agent by name (if it exists)."""
    r = requests.delete(_url(f"agents/{name}"), headers=_headers())
    if r.status_code in (200, 204):
        print(f"  Deleted existing agent: {name}")
    elif r.status_code == 404:
        pass  # doesn't exist yet
    else:
        print(f"  Delete {name}: {r.status_code} {r.text}")


def create_agent(name: str, instructions: str) -> str:
    """Create a new-style Foundry agent via POST /agents."""
    _delete_new_agent(name)
    payload = {
        "name": name,
        "definition": {
            "kind": "prompt",
            "model": MODEL,
            "instructions": instructions,
        },
    }
    r = requests.post(_url("agents"), headers=_headers(), json=payload)
    r.raise_for_status()
    print(f"  Created {name}")
    return name


def create_workflow(name: str, agent_names: list[str]) -> str:
    """Create a Workflow agent referencing the given agents in sequence."""
    _delete_new_agent(name)

    # Build actions list in the portal-compatible YAML schema
    # Uses InvokeAzureAgent kind (shows agent name in visualizer)
    # with conversationId chaining so each agent receives prior context
    actions_yaml = ""
    for agent in agent_names:
        actions_yaml += (
            f"    - kind: InvokeAzureAgent\n"
            f"      id: node-{agent}\n"
            f"      agent:\n"
            f"        name: {agent}\n"
            f"      conversationId: =System.ConversationId\n"
            f"      input:\n"
            f'        messages: ""\n'
            f"      output:\n"
            f"        autoSend: true\n"
        )

    workflow_yaml = (
        "kind: workflow\n"
        f"name: {name}\n"
        "trigger:\n"
        "  kind: OnConversationStart\n"
        "  id: trigger-main\n"
        "  actions:\n"
        f"{actions_yaml}"
        'id: ""\n'
        'description: "Mortgage Loan Origination multi-agent workflow"\n'
    )
    payload = {
        "name": name,
        "definition": {
            "kind": "workflow",
            "workflow": workflow_yaml,
        },
    }
    r = requests.post(_url("agents"), headers=_headers(), json=payload)
    r.raise_for_status()
    print(f"  Created workflow: {name}")
    return name


AGENTS = [
    ("IntakeAgent",        INTAKE_INSTRUCTIONS),
    ("DocumentAgent",      DOCUMENT_INSTRUCTIONS),
    ("UnderwritingAgent",  UNDERWRITING_INSTRUCTIONS),
    ("RiskAgent",          RISK_INSTRUCTIONS),
    ("ComplianceAgent",    COMPLIANCE_INSTRUCTIONS),
    ("CommsAgent",         COMMS_INSTRUCTIONS),
    ("OrchestratorAgent",  ORCHESTRATOR_INSTRUCTIONS),
]


def main():
    # 1. Delete any existing agents (idempotent re-run)
    print("Cleaning up existing agents …")
    r = requests.get(_url("agents"), headers=_headers())
    if r.status_code == 200:
        for a in r.json().get("data", []):
            _delete_new_agent(a["name"])

    # 2. Create new-style agents
    print("\nCreating new Foundry agents …")
    names = {}
    for name, instructions in AGENTS:
        create_agent(name, instructions)
        key = name.replace("Agent", "").lower() + "_name"
        names[key] = name

    # 3. Create the workflow
    print("\nCreating workflow …")
    agent_order = [a[0] for a in AGENTS]
    create_workflow("MortgageLoanOrigination", agent_order)
    names["workflow_name"] = "MortgageLoanOrigination"

    # 4. Verify
    print("\nVerifying …")
    r = requests.get(_url("agents"), headers=_headers())
    agents = r.json().get("data", [])
    print(f"  Total agents in project: {len(agents)}")
    for a in agents:
        kind = a.get("versions", {}).get("latest", {}).get("definition", {}).get("kind", "?")
        print(f"    {a['name']} ({kind})")

    # 5. Persist agent names for api service & run_workflow
    out_path = Path(__file__).resolve().parent / "agent_ids.json"
    out_path.write_text(json.dumps(names, indent=2))
    print(f"\nAgent names saved to {out_path}")
    print(json.dumps(names, indent=2))


if __name__ == "__main__":
    main()
