"""
run_workflow.py – Local runner that executes the mortgage workflow
by calling each specialist agent in sequence via the Foundry Responses
API and consolidating via the OrchestratorAgent.

Usage:
    python src/foundry/run_workflow.py data/samples/application_1.json
"""

import json
import os
import sys
from pathlib import Path

import requests as http_requests
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

ENDPOINT = os.environ["PROJECT_ENDPOINT"].rstrip("/")
MODEL = os.environ.get("MODEL_DEPLOYMENT", "gpt-4o")
API_VERSION = "2025-05-15-preview"
AGENT_IDS_PATH = Path(__file__).resolve().parent / "agent_ids.json"

_credential = DefaultAzureCredential()


def _get_headers() -> dict:
    token = _credential.get_token("https://ai.azure.com/.default").token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def load_application(path: str) -> dict:
    return json.loads(Path(path).read_text())


def load_docs_metadata(app_id: str) -> list:
    """Load document metadata for a given application id."""
    meta_path = Path(__file__).resolve().parents[2] / "data" / "samples" / "docs_metadata.json"
    meta = json.loads(meta_path.read_text())
    if meta.get("application_id") == app_id:
        return meta.get("documents", [])
    return meta.get("_app2_documents", meta.get("documents", []))


def load_doc_texts() -> dict[str, str]:
    """Load synthetic document text snippets."""
    texts_dir = Path(__file__).resolve().parents[2] / "data" / "samples" / "sample_doc_texts"
    result = {}
    if texts_dir.exists():
        for f in texts_dir.iterdir():
            if f.is_file():
                result[f.name] = f.read_text()
    return result


def _call_agent(agent_name: str, message: str, timeout: int = 120, is_workflow: bool = False) -> str:
    """Send *message* to a Foundry agent via the Responses API and return the text reply."""
    headers = _get_headers()
    payload = {
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
        print(f"  Created conversation {conv_id}")

    url = f"{ENDPOINT}/openai/responses?api-version={API_VERSION}"
    r = http_requests.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    texts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    texts.append(c.get("text", ""))
        elif item.get("type") == "workflow_action":
            action_output = item.get("output", "")
            if action_output:
                texts.append(action_output if isinstance(action_output, str) else json.dumps(action_output))
    return texts[-1] if texts else "{}"
    return texts[-1] if texts else "{}"


def run(application_path: str) -> dict:
    application = load_application(application_path)
    app_id = application.get("application_id", "UNKNOWN")
    docs_meta = load_docs_metadata(app_id)
    doc_texts = load_doc_texts()

    # Enrich docs with text snippets
    for doc in docs_meta:
        fname = doc.get("filename")
        if fname and fname in doc_texts:
            doc["text_snippet"] = doc_texts[fname]

    agent_names = json.loads(AGENT_IDS_PATH.read_text())
    workflow_name = agent_names.get("workflow_name", "MortgageLoanOrigination")

    payload_json = json.dumps({"application": application, "documents": docs_meta}, indent=2)

    print(f"  Invoking workflow {workflow_name} …")
    raw_response = _call_agent(workflow_name, payload_json, timeout=600, is_workflow=True)

    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError:
        result = {"raw_response": raw_response}
    result.setdefault("application_id", app_id)
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_workflow.py <application.json>")
        sys.exit(1)
    result = run(sys.argv[1])
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
