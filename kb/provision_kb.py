"""Provision the Foundry IQ knowledge base over the MedQuAD corpus.

Idempotent; called by the azd postprovision hook. Creates/updates, in Azure AI
Search:
  1. index ``medquad`` (semantic config, RBAC-only service)
  2. documents from data/corpus.jsonl (built by data/ingest.py)
  3. knowledge source ``medquad-ks`` over the index
  4. knowledge base ``clinical-kb`` (extractive data output: the agent's own
     model writes the answer, the KB returns evidence + source ids)

Stores the KB MCP endpoint as KB_MCP_ENDPOINT in the azd environment.

Env: AZURE_SEARCH_ENDPOINT, AZURE_OPENAI_ENDPOINT, AZURE_AI_MODEL_DEPLOYMENT_NAME
Identity needs Search Service Contributor + Search Index Data Contributor.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from foundry_agents import make_credential  # noqa: E402

CORPUS = ROOT / "data" / "corpus.jsonl"

SEARCH_SCOPE = "https://search.azure.com/.default"
API_VERSION = "2026-05-01-preview"
SEMANTIC_CONFIG = "default"
INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX_NAME", "medquad")
KS_NAME = os.environ.get("KNOWLEDGE_SOURCE_NAME", "medquad-ks")
KB_NAME = os.environ.get("KNOWLEDGE_BASE_NAME", "clinical-kb")
BATCH = 500


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"ERROR: required environment variable '{name}' is not set.")
    return value


class Search:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.credential = make_credential()

    def _headers(self) -> dict[str, str]:
        token = self.credential.get_token(SEARCH_SCOPE).token
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def request(self, method: str, path: str, body: dict | None = None, ok=(200, 201, 204)) -> dict:
        url = f"{self.endpoint}/{path}?api-version={API_VERSION}"
        for attempt in range(6):
            resp = requests.request(method, url, headers=self._headers(), json=body, timeout=180)
            # RBAC assignments made moments ago by Terraform can take a few minutes to propagate.
            if resp.status_code in (401, 403, 429, 503) and attempt < 5:
                wait = 20 * (attempt + 1)
                print(f"  {method} {path} -> {resp.status_code}; retrying in {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code not in ok:
                sys.exit(f"ERROR: {method} {path} failed ({resp.status_code}): {resp.text[:2000]}")
            return resp.json() if resp.content else {}
        return {}


def create_index(search: Search) -> None:
    print(f"Index '{INDEX_NAME}'...")
    searchable = {"type": "Edm.String", "searchable": True, "retrievable": True}
    search.request(
        "PUT",
        f"indexes/{INDEX_NAME}",
        {
            "name": INDEX_NAME,
            "fields": [
                {"name": "id", "type": "Edm.String", "key": True, "filterable": True, "retrievable": True},
                {"name": "source_id", "type": "Edm.String", "filterable": True, "retrievable": True, "searchable": False},
                {"name": "title", **searchable},
                {"name": "focus", **searchable, "filterable": True},
                {"name": "question", **searchable},
                {"name": "qtype", "type": "Edm.String", "filterable": True, "facetable": True, "retrievable": True},
                {"name": "content", **searchable, "analyzer": "en.microsoft"},
                {"name": "url", "type": "Edm.String", "retrievable": True},
                {"name": "collection", "type": "Edm.String", "filterable": True, "facetable": True, "retrievable": True},
            ],
            "semantic": {
                "defaultConfiguration": SEMANTIC_CONFIG,
                "configurations": [
                    {
                        "name": SEMANTIC_CONFIG,
                        "prioritizedFields": {
                            "titleField": {"fieldName": "title"},
                            "prioritizedContentFields": [{"fieldName": "content"}],
                            "prioritizedKeywordsFields": [{"fieldName": "focus"}, {"fieldName": "question"}],
                        },
                    }
                ],
            },
        },
    )


def upload(search: Search) -> int:
    if not CORPUS.exists():
        subprocess.run([sys.executable, str(ROOT / "data" / "ingest.py")], check=True)
    docs = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"Uploading {len(docs)} documents...")
    for i in range(0, len(docs), BATCH):
        actions = [{"@search.action": "mergeOrUpload", **d} for d in docs[i : i + BATCH]]
        search.request("POST", f"indexes/{INDEX_NAME}/docs/index", {"value": actions}, ok=(200, 201, 207))
    return len(docs)


def create_knowledge_source(search: Search) -> None:
    print(f"Knowledge source '{KS_NAME}'...")
    search.request(
        "PUT",
        f"knowledgesources/{KS_NAME}",
        {
            "name": KS_NAME,
            "kind": "searchIndex",
            "description": "MedQuAD NIH consumer health Q&A (CC BY 4.0): conditions, symptoms, causes, treatment, prevention.",
            "searchIndexParameters": {
                "searchIndexName": INDEX_NAME,
                "semanticConfigurationName": SEMANTIC_CONFIG,
                "sourceDataFields": [
                    {"name": "source_id"},
                    {"name": "title"},
                    {"name": "url"},
                    {"name": "content"},
                ],
                "searchFields": [],
            },
        },
    )


def create_knowledge_base(search: Search) -> None:
    print(f"Knowledge base '{KB_NAME}'...")
    deployment = require("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    search.request(
        "PUT",
        f"knowledgebases/{KB_NAME}",
        {
            "name": KB_NAME,
            "description": "Clinical reference knowledge base (historical NIH consumer-health content).",
            "knowledgeSources": [{"name": KS_NAME}],
            "outputMode": "extractiveData",
            "retrievalReasoningEffort": {"kind": "low"},
            "models": [
                {
                    "kind": "azureOpenAI",
                    "azureOpenAIParameters": {
                        "resourceUri": require("AZURE_OPENAI_ENDPOINT").rstrip("/"),
                        "deploymentId": deployment,
                        "modelName": os.environ.get("AZURE_AI_MODEL_NAME", "").strip() or deployment,
                    },
                }
            ],
        },
    )


def set_azd_env(name: str, value: str) -> bool:
    azd = shutil.which("azd")
    if not azd:
        return False
    try:
        subprocess.run([azd, "env", "set", name, value], check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def main() -> None:
    endpoint = require("AZURE_SEARCH_ENDPOINT")
    search = Search(endpoint)
    create_index(search)
    count = upload(search)
    create_knowledge_source(search)
    create_knowledge_base(search)
    mcp = f"{endpoint.rstrip('/')}/knowledgebases/{KB_NAME}/mcp?api-version={API_VERSION}"
    print(f"Knowledge base '{KB_NAME}' ready ({count} docs). MCP endpoint: {mcp}")
    if not set_azd_env("KB_MCP_ENDPOINT", mcp):
        print(f'Set it manually: azd env set KB_MCP_ENDPOINT "{mcp}"')


if __name__ == "__main__":
    main()
