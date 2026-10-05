"""Deterministic input checks that run before the model is called.

Pure functions only (no I/O) so they are unit-testable and cheap. Model-side safety
(content filters, Prompt Shields) is enforced by the Foundry guardrail; these checks
cover app-specific policy the platform does not know about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_INPUT_CHARS = 2000

# Patterns that strongly suggest the user pasted protected health information.
_PHI_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("mrn", re.compile(r"\b(?:MRN|medical record (?:number|no\.?|#))\s*[:#]?\s*[A-Z0-9-]{5,}\b", re.I)),
    ("dob", re.compile(r"\b(?:DOB|date of birth)\s*[:#]?\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", re.I)),
    ("phone", re.compile(r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
]

_CRISIS_PATTERNS = re.compile(
    r"\b(?:kill myself|end my life|suicid(?:e|al)|want to die|hurt myself|self[- ]harm(?:ing)?|overdose on purpose)\b",
    re.I,
)

_EMERGENCY_PATTERNS = re.compile(
    r"\b(?:(?<!risk of )(?<!chance of )(?<!odds of )(?<!prevent )(?<!avoid )(?<!from )"
    r"(?:having|have) (?:a )?(?:heart attack|stroke|seizure)|can'?t breathe|(?:chest pain|stroke symptoms) (?:right )?now|"
    r"unconscious|not breathing|anaphyla(?:xis|ctic))\b",
    re.I,
)

_HARM_PATTERNS = re.compile(
    r"\b(?:(?:make|making|create|produce|mix|mixing|combine)\b[^.?!]{0,60}\b(?:toxic|poison(?:ous)?|deadly|lethal)\s+"
    r"(?:gas|gases|fumes|chemicals?|substances?|dose)|"
    r"how (?:to|can i|do i|could i) (?:poison|kill|seriously harm) (?:someone|somebody|a person|my|him|her|them)|"
    r"(?:obtain|get|buy)\b[^.?!]{0,40}\b(?:opioids?|oxycodone|fentanyl|controlled substances?)\b[^.?!]{0,20}"
    r"without (?:a )?prescription)",
    re.I,
)

REFUSAL_PHI = (
    "I can't process messages that contain patient identifiers (for example SSNs, MRNs, dates of birth, "
    "phone numbers or email addresses). Please remove identifying details and ask a general clinical "
    "knowledge question."
)
REFUSAL_CRISIS = (
    "It sounds like you or someone else may be in crisis. I'm not able to help with that here. "
    "In the U.S., call or text **988** (Suicide & Crisis Lifeline) or call **911** for immediate danger."
)
REFUSAL_EMERGENCY = (
    "This sounds like a possible medical emergency. Call **911** (or your local emergency number) now. "
    "I provide general reference information only and can't help with emergencies."
)
REFUSAL_EMPTY = "Please enter a clinical knowledge question."
REFUSAL_HARMFUL = (
    "I can't help with that. If someone may have been exposed to a poison, call Poison Control at "
    "**1-800-222-1222** or **911** for an emergency."
)
REFUSAL_TOO_LONG = f"Your message is too long. Please keep questions under {MAX_INPUT_CHARS} characters."


@dataclass(frozen=True)
class CheckResult:
    allowed: bool
    reason: str = "ok"
    message: str = ""


def check_input(text: str | None) -> CheckResult:
    """Return whether the user input may be sent to the model, and why not if blocked."""
    text = (text or "").strip()
    if not text:
        return CheckResult(False, "empty", REFUSAL_EMPTY)
    if len(text) > MAX_INPUT_CHARS:
        return CheckResult(False, "too_long", REFUSAL_TOO_LONG)
    if _CRISIS_PATTERNS.search(text):
        return CheckResult(False, "crisis", REFUSAL_CRISIS)
    if _EMERGENCY_PATTERNS.search(text):
        return CheckResult(False, "emergency", REFUSAL_EMERGENCY)
    if _HARM_PATTERNS.search(text):
        return CheckResult(False, "harmful", REFUSAL_HARMFUL)
    for name, pattern in _PHI_PATTERNS:
        if pattern.search(text):
            return CheckResult(False, f"phi_{name}", REFUSAL_PHI)
    return CheckResult(True)
