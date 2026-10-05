"""Clinical Knowledge Assistant: a Foundry hosted agent grounded on a Foundry IQ knowledge base.

Request pipeline (all in-process, inside the hosted agent container):

    user input -> InputGuard (deterministic PHI / crisis / scope checks)
               -> Agent (gpt model + Foundry IQ knowledge base via toolbox MCP)
               -> CitationValidator (citations must match retrieved KB docs; trailer metadata)

Model-level safety (content filters, Prompt Shields) is enforced by the Foundry guardrail on the
model deployment. Traces flow to Application Insights via the hosting adapter's OpenTelemetry
instrumentation; this module adds `clinical.*` span attributes for governance dashboards.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import checks
import validator
from agent_framework import (
    Agent,
    AgentContext,
    AgentMiddleware,
    AgentResponse,
    AgentResponseUpdate,
    Content,
    FunctionInvocationContext,
    FunctionMiddleware,
    Message,
    ResponseStream,
)
from agent_framework.exceptions import ChatClientContentFilterException
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import FoundryToolbox, ResponsesHostServer
from azure.identity import DefaultAzureCredential
from opentelemetry import trace

logger = logging.getLogger("clinical_agent")
tracer = trace.get_tracer("clinical_agent")

PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"
_PROMPT_VERSION_RE = re.compile(r"<!--\s*PROMPT_VERSION:\s*([\w.\-]+)\s*-->")

# Collects raw KB tool output for the current run. A mutable list is stored so that tool calls
# executed in child tasks (which copy the context) still append to the same list.
_tool_outputs: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("tool_outputs", default=None)

GUARDRAIL_MESSAGE = (
    "This request was blocked by the Foundry guardrail (content safety / Prompt Shields). "
    "I can help with general clinical reference questions instead."
)
GUARDRAIL_REASON = "guardrail_content_filter"


def is_content_filter(exc: BaseException) -> bool:
    """True when the model call was rejected by the deployment's guardrail (content filter / Prompt Shields)."""
    if isinstance(exc, ChatClientContentFilterException):
        return True
    text = str(exc).lower()
    return "content_filter" in text or "content management policy" in text


def load_prompt() -> tuple[str, str]:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    match = _PROMPT_VERSION_RE.search(text)
    version = match.group(1) if match else "unknown"
    return _PROMPT_VERSION_RE.sub("", text).strip(), version


def run_meta(prompt_version: str) -> dict[str, Any]:
    return {
        "prompt_version": prompt_version,
        "agent_git_sha": os.environ.get("AGENT_GIT_SHA", "local"),
    }


def _last_user_text(messages: list[Message]) -> str:
    for message in reversed(messages or []):
        if str(getattr(message, "role", "")).lower().endswith("user"):
            return message.text or ""
    return ""


def _content_output_text(content: Content) -> str:
    for attr in ("result", "output"):
        value = getattr(content, attr, None)
        if value:
            return value if isinstance(value, str) else str(value)
    return ""


class KnowledgeCaptureMiddleware(FunctionMiddleware):
    """Records raw tool output so the validator knows which KB documents were retrieved."""

    async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        await call_next()
        sink = _tool_outputs.get()
        if sink is not None and context.result is not None:
            result = context.result
            if isinstance(result, list):
                sink.extend(str(getattr(c, "text", None) or c) for c in result)
            else:
                sink.append(str(result))


class ClinicalGuardMiddleware(AgentMiddleware):
    """Input guard + citation validator around the model/tool loop."""

    def __init__(self, prompt_version: str) -> None:
        self._prompt_version = prompt_version

    async def process(self, context: AgentContext, call_next: Callable[[], Awaitable[None]]) -> None:
        meta = run_meta(self._prompt_version)
        user_text = _last_user_text(context.messages)
        verdict = checks.check_input(user_text)

        span = trace.get_current_span()
        span.set_attribute("clinical.prompt_version", self._prompt_version)
        span.set_attribute("clinical.input_check", verdict.reason)

        if not verdict.allowed:
            logger.info("input blocked: %s", verdict.reason)
            text = validator.refusal(verdict.message, verdict.reason, meta)
            context.result = self._static_result(text, context.stream)
            return

        sink: list[str] = []
        token = _tool_outputs.set(sink)
        try:
            await call_next()
        except Exception as exc:
            if not is_content_filter(exc):
                raise
            context.result = self._static_result(self._guardrail_refusal(meta), context.stream)
            return
        finally:
            if not context.stream:
                _tool_outputs.reset(token)

        if not context.stream and getattr(context.result, "finish_reason", None) == "content_filter":
            context.result = self._static_result(self._guardrail_refusal(meta), False)
            return

        if context.stream:
            context.result = self._validated_stream(context.result, sink, meta)  # type: ignore[arg-type]
        else:
            response: AgentResponse = context.result  # type: ignore[assignment]
            for message in response.messages:
                for content in message.contents:
                    if content.type in ("function_result", "mcp_server_tool_result"):
                        sink.append(_content_output_text(content))
            final = self._finalize(response.text or "", sink, meta)
            context.result = AgentResponse(
                messages=[Message("assistant", [Content.from_text(final)])],
                response_id=response.response_id,
                usage_details=response.usage_details,
            )

    @staticmethod
    def _guardrail_refusal(meta: dict[str, Any]) -> str:
        logger.warning("model call blocked by guardrail content filter")
        span = trace.get_current_span()
        span.set_attribute("clinical.guardrail_blocked", True)
        span.set_attribute("clinical.evidence_status", "refused")
        return validator.refusal(GUARDRAIL_MESSAGE, GUARDRAIL_REASON, meta)

    def _finalize(self, answer: str, sink: list[str], meta: dict[str, Any]) -> str:
        with tracer.start_as_current_span("clinical.validate") as span:
            retrieved = validator.extract_sources(sink)
            result = validator.validate_answer(answer, retrieved)
            span.set_attribute("clinical.prompt_version", meta["prompt_version"])
            span.set_attribute("clinical.evidence_status", result.evidence_status)
            span.set_attribute("clinical.validation", result.validation)
            span.set_attribute("clinical.retrieved_count", len(result.retrieved))
            span.set_attribute("clinical.cited_count", len(result.cited))
            span.set_attribute("clinical.removed_citations", len(result.removed_citations))
            if result.validation != "ok":
                logger.warning("validator %s: removed=%s", result.validation, result.removed_citations)
            return validator.render(result, retrieved, meta)

    @staticmethod
    def _static_result(text: str, stream: bool) -> AgentResponse | ResponseStream[AgentResponseUpdate, AgentResponse]:
        if not stream:
            return AgentResponse(messages=[Message("assistant", [Content.from_text(text)])])

        async def _one() -> AsyncIterator[AgentResponseUpdate]:
            yield AgentResponseUpdate(contents=[Content.from_text(text)], role="assistant")

        return ResponseStream(_one(), finalizer=AgentResponse.from_updates)

    def _validated_stream(
        self,
        inner: ResponseStream[AgentResponseUpdate, AgentResponse],
        sink: list[str],
        meta: dict[str, Any],
    ) -> ResponseStream[AgentResponseUpdate, AgentResponse]:
        """Buffer model text, pass tool activity through, then emit one validated message."""

        async def _gen() -> AsyncIterator[AgentResponseUpdate]:
            token = _tool_outputs.set(sink)
            try:
                text_parts: list[str] = []
                message_id: str | None = None
                response_id: str | None = None
                filtered = False
                try:
                    async for update in inner:
                        if getattr(update, "finish_reason", None) == "content_filter":
                            filtered = True
                        if any(c.type == "text" for c in update.contents):
                            message_id = update.message_id or message_id
                        passthrough = self._split_update(update, text_parts, sink)
                        response_id = update.response_id or response_id
                        if passthrough:
                            yield AgentResponseUpdate(
                                contents=passthrough,
                                role=update.role,
                                message_id=update.message_id,
                                response_id=update.response_id,
                            )
                except Exception as exc:
                    if not is_content_filter(exc):
                        raise
                    filtered = True
                if filtered:
                    yield AgentResponseUpdate(
                        contents=[Content.from_text(self._guardrail_refusal(meta))],
                        role="assistant",
                        message_id=message_id,
                        response_id=response_id,
                    )
                    return
                try:
                    await inner.get_final_response()
                except Exception:  # finalization only feeds telemetry hooks
                    logger.debug("inner stream finalization failed", exc_info=True)
                final = self._finalize("".join(text_parts), sink, meta)
                yield AgentResponseUpdate(
                    contents=[Content.from_text(final)],
                    role="assistant",
                    message_id=message_id,
                    response_id=response_id,
                )
            finally:
                _tool_outputs.reset(token)

        return ResponseStream(_gen(), finalizer=AgentResponse.from_updates)

    @staticmethod
    def _split_update(update: AgentResponseUpdate, text_parts: list[str], sink: list[str]) -> list[Content]:
        """Buffer text, drop reasoning, record tool output; return contents to pass through."""
        passthrough: list[Content] = []
        for content in update.contents:
            if content.type == "text":
                text_parts.append(content.text or "")
            elif content.type == "text_reasoning":
                continue
            else:
                if content.type in ("function_result", "mcp_server_tool_result"):
                    sink.append(_content_output_text(content))
                passthrough.append(content)
        return passthrough


def build_agent(credential: Any | None = None) -> Agent:
    credential = credential or DefaultAzureCredential()
    instructions, prompt_version = load_prompt()
    toolbox = FoundryToolbox(credential, name="knowledge_base")
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=credential,
    )
    return Agent(
        client=client,
        name="clinical-agent",
        instructions=instructions,
        tools=toolbox,
        middleware=[ClinicalGuardMiddleware(prompt_version), KnowledgeCaptureMiddleware()],
        default_options={"store": False},
    )


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    server = ResponsesHostServer(build_agent())
    await server.run_async()


if __name__ == "__main__":
    asyncio.run(main())
