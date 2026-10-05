"""Deterministic post-generation validation of agent answers.

Pure functions only. The validator enforces the output contract:

* every citation ``[mq-...]`` in the answer must refer to a document that the knowledge
  base actually returned for this turn (no fabricated citations);
* a substantive answer must cite at least one retrieved document, otherwise it is replaced
  by a safe "insufficient evidence" response;
* a machine-readable trailer is appended so the UI and evaluators can see what happened.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SOURCE_ID_RE = re.compile(r"mq-[a-z]+-[0-9a-z]+-\d+")
CITATION_RE = re.compile(r"\[(mq-[a-z]+-[0-9a-z]+-\d+)\]")
TRAILER_RE = re.compile(r"\n*<!-- meta: (\{.*?\}) -->\s*$", re.S)
SOURCES_HEADER_RE = re.compile(r"\n+#{0,3}\s*\**\s*(?:Sources|References)\s*:?\s*\**\s*:?\s*\n.*$", re.S | re.I)

ABSTAIN_MARKER = "I don't have enough information in the knowledge base"
FALLBACK_ANSWER = (
    f"{ABSTAIN_MARKER} to answer that reliably. Please consult a qualified clinician or an "
    "authoritative clinical reference."
)
DISCLAIMER = "_General reference information from NIH/MedlinePlus sources, not medical advice._"


@dataclass
class SourceDoc:
    source_id: str
    title: str = ""
    url: str = ""


@dataclass
class Validation:
    answer: str
    evidence_status: str  # grounded | insufficient | refused
    validation: str  # ok | repaired_citations | blocked_ungrounded | refused
    retrieved: list[str] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)
    removed_citations: list[str] = field(default_factory=list)


def _walk(node: Any, docs: dict[str, SourceDoc]) -> None:
    if isinstance(node, dict):
        sid = node.get("source_id") or node.get("sourceId")
        if isinstance(sid, str) and SOURCE_ID_RE.fullmatch(sid):
            doc = docs.setdefault(sid, SourceDoc(sid))
            doc.title = doc.title or str(node.get("title") or "")
            doc.url = doc.url or str(node.get("url") or "")
        for value in node.values():
            _walk(value, docs)
    elif isinstance(node, list):
        for item in node:
            _walk(item, docs)
    elif isinstance(node, str):
        stripped = node.strip()
        if stripped[:1] in ("{", "[") and "mq-" in stripped:
            try:
                _walk(json.loads(stripped), docs)
            except ValueError:
                pass


def extract_sources(tool_outputs: list[str]) -> dict[str, SourceDoc]:
    """Extract retrieved document ids (and title/url when available) from raw KB tool output."""
    docs: dict[str, SourceDoc] = {}
    for raw in tool_outputs:
        if not raw:
            continue
        _walk(raw, docs)
        for sid in SOURCE_ID_RE.findall(raw):
            docs.setdefault(sid, SourceDoc(sid))
    return docs


def strip_trailer(text: str) -> tuple[str, dict[str, Any] | None]:
    """Split an answer into (visible text, meta dict)."""
    match = TRAILER_RE.search(text or "")
    if not match:
        return text or "", None
    try:
        meta = json.loads(match.group(1))
    except ValueError:
        meta = None
    return text[: match.start()].rstrip(), meta


def _ordered_unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def is_abstention(text: str) -> bool:
    normalized = (text or "").replace("\u2019", "'").lower()
    return ABSTAIN_MARKER.lower() in normalized


def validate_answer(answer: str, retrieved: dict[str, SourceDoc]) -> Validation:
    """Validate citations against retrieved documents and rewrite the answer if needed."""
    body = SOURCES_HEADER_RE.sub("", (answer or "").strip()).strip()
    retrieved_ids = list(retrieved)
    cited = _ordered_unique(CITATION_RE.findall(body))
    valid = [c for c in cited if c in retrieved]
    invalid = [c for c in cited if c not in retrieved]

    if invalid:
        for bad in invalid:
            body = body.replace(f"[{bad}]", "")
        body = re.sub(r"[ \t]+([.,;:])", r"\1", body)

    if is_abstention(body):
        return Validation(
            answer=body,
            evidence_status="insufficient",
            validation="repaired_citations" if invalid else "ok",
            retrieved=retrieved_ids,
            cited=valid,
            removed_citations=invalid,
        )

    if not valid:
        return Validation(
            answer=FALLBACK_ANSWER,
            evidence_status="insufficient",
            validation="blocked_ungrounded",
            retrieved=retrieved_ids,
            cited=[],
            removed_citations=invalid,
        )

    return Validation(
        answer=body,
        evidence_status="grounded",
        validation="repaired_citations" if invalid else "ok",
        retrieved=retrieved_ids,
        cited=valid,
        removed_citations=invalid,
    )


def render(result: Validation, retrieved: dict[str, SourceDoc], meta: dict[str, Any]) -> str:
    """Render the final markdown answer: body, Sources list, disclaimer and meta trailer."""
    parts = [result.answer.strip()]
    if result.cited:
        lines = ["**Sources**"]
        for sid in result.cited:
            doc = retrieved.get(sid, SourceDoc(sid))
            label = doc.title or sid
            lines.append(f"- [{sid}] [{label}]({doc.url})" if doc.url else f"- [{sid}] {label}")
        parts.append("\n".join(lines))
    if result.evidence_status != "refused":
        parts.append(DISCLAIMER)
    trailer = {
        "evidence_status": result.evidence_status,
        "validation": result.validation,
        "retrieved": result.retrieved,
        "cited": result.cited,
        "removed_citations": result.removed_citations,
        **meta,
    }
    return "\n\n".join(parts) + f"\n\n<!-- meta: {json.dumps(trailer, separators=(',', ':'))} -->"


def refusal(message: str, reason: str, meta: dict[str, Any]) -> str:
    result = Validation(answer=message, evidence_status="refused", validation="refused")
    return render(result, {}, {**meta, "refusal_reason": reason})
