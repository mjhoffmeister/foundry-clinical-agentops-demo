"""Clinical knowledge chat UI.

FastAPI app behind App Service Easy Auth. The site's managed identity calls the
hosted agent's production endpoint; the version selector decides which agent
version serves traffic, so promotions and rollbacks need no web redeploy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from opentelemetry import trace
from pydantic import BaseModel, Field

if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(logger_name="clinical_web")

log = logging.getLogger("clinical_web")
tracer = trace.get_tracer("clinical_web")

AGENT_NAME = os.environ.get("AGENT_NAME", "clinical-agent")
PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
API = "v1"
SCOPE = "https://ai.azure.com/.default"
TRAILER_RE = re.compile(r"\n*<!-- meta: (\{.*?\}) -->\s*$", re.S)
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Clinical Knowledge Assistant", docs_url=None, redoc_url=None)
_credential = DefaultAzureCredential()
_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
_info_cache: dict = {"at": 0.0, "value": None}


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


def split_trailer(text: str) -> tuple[str, dict]:
    m = TRAILER_RE.search(text or "")
    if not m:
        return (text or "").strip(), {}
    try:
        meta = json.loads(m.group(1))
    except json.JSONDecodeError:
        meta = {}
    return text[: m.start()].rstrip(), meta


def output_text(payload: dict) -> str:
    if payload.get("output_text"):
        return payload["output_text"]
    parts = []
    for item in payload.get("output") or []:
        if item.get("type") == "message":
            parts += [c.get("text", "") for c in item.get("content") or [] if c.get("type") in ("output_text", "text")]
    return "\n".join(parts)


def user_hash(request: Request) -> str:
    principal = request.headers.get("x-ms-client-principal-id") or request.headers.get("x-ms-client-principal-name")
    if not principal:
        return "anonymous"
    return "u-" + hashlib.sha256(principal.encode()).hexdigest()[:16]


async def _headers() -> dict[str, str]:
    token = await _credential.get_token(SCOPE)
    return {"Authorization": "Bearer " + token.token, "Content-Type": "application/json"}


async def pinned_version() -> str | None:
    if time.time() - _info_cache["at"] < 30:
        return _info_cache["value"]
    resp = await _client.get(f"{PROJECT_ENDPOINT}/agents/{AGENT_NAME}", params={"api-version": API},
                             headers=await _headers())
    resp.raise_for_status()
    rules = ((resp.json().get("agent_endpoint") or {}).get("version_selector") or {}).get("version_selection_rules") or []
    pinned = [r for r in rules if int(r.get("traffic_percentage", 0)) == 100]
    value = str(pinned[0]["agent_version"]) if len(pinned) == 1 else None
    _info_cache.update(at=time.time(), value=value)
    return value


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/info")
async def info(request: Request) -> dict:
    try:
        version = await pinned_version()
    except httpx.HTTPError as exc:
        log.warning("selector lookup failed: %s", exc)
        version = None
    return {
        "agent": AGENT_NAME,
        "production_version": version,
        "user": request.headers.get("x-ms-client-principal-name", "anonymous"),
    }


@app.post("/api/chat")
async def chat(body: ChatRequest, request: Request) -> JSONResponse:
    uid = user_hash(request)
    with tracer.start_as_current_span("clinical.chat") as span:
        span.set_attribute("enduser.id", uid)
        span.set_attribute("clinical.purpose", "clinical-reference-lookup")
        span.set_attribute("gen_ai.agent.name", AGENT_NAME)
        started = time.perf_counter()
        try:
            resp = await _client.post(
                f"{PROJECT_ENDPOINT}/agents/{AGENT_NAME}/endpoint/protocols/openai/responses",
                params={"api-version": API},
                headers=await _headers(),
                json={"input": body.question, "stream": False, "user": uid},
            )
        except httpx.HTTPError as exc:
            span.record_exception(exc)
            raise HTTPException(502, "The agent endpoint is unreachable. Try again shortly.") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        trace_id = format(span.get_span_context().trace_id, "032x")

        if resp.status_code >= 400:
            text = resp.text
            blocked = "content_filter" in text or "content management policy" in text.lower()
            span.set_attribute("clinical.blocked_by_guardrail", blocked)
            if blocked:
                return JSONResponse({
                    "answer": "This request was blocked by the Foundry guardrail (content safety / prompt shields).",
                    "meta": {"evidence_status": "blocked", "validation": "guardrail"},
                    "version": None, "response_id": None, "trace_id": trace_id, "latency_ms": latency_ms,
                })
            log.error("agent error %s: %s", resp.status_code, text[:500])
            raise HTTPException(502, f"Agent returned HTTP {resp.status_code}")

        payload = resp.json()
        answer, meta = split_trailer(output_text(payload))
        version = resp.headers.get("x-ms-agent-version") or (payload.get("agent_reference") or {}).get("version")
        if not version:
            try:
                version = await pinned_version()
            except httpx.HTTPError:
                version = None
        span.set_attribute("clinical.evidence_status", meta.get("evidence_status", "unknown"))
        span.set_attribute("clinical.blocked_by_guardrail", str(meta.get("refusal_reason", "")).startswith("guardrail"))
        span.set_attribute("clinical.agent_version", str(version or meta.get("agent_version") or ""))
        span.set_attribute("gen_ai.response.id", payload.get("id") or "")
        return JSONResponse({
            "answer": answer,
            "meta": meta,
            "version": version,
            "response_id": payload.get("id"),
            "trace_id": trace_id,
            "latency_ms": latency_ms,
        })
