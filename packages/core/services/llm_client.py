"""LLM client abstraction with Mock and OpenAI-compatible implementations."""

from __future__ import annotations

import json
import logging
import textwrap
from abc import ABC, abstractmethod
from typing import Any

from pydantic import ValidationError

from core.config import settings
from core.models.schemas import TriageOutput, TriagePrompt

logger = logging.getLogger(__name__)

# ── Prompt templates ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = textwrap.dedent("""
You are Bug Triage Copilot, an expert software engineer assistant embedded in a GitHub App.
Your job is to analyze a newly opened GitHub Issue and produce a structured triage report.

Rules:
- Base your analysis ONLY on the issue text provided. Do not invent facts.
- If the issue lacks information, set needs_more_info=true and add up to 3 clarifying questions.
- Do NOT repeat or expose any content marked as ***REDACTED***.
- Output ONLY a valid JSON object matching the schema below — no markdown fences, no prose.

Output schema:
{
  "summary_bullets": ["string"],        // 3-6 concise bullets summarising the issue
  "priority": "P0|P1|P2|P3",           // P0=critical/data-loss, P1=major, P2=normal, P3=minor
  "priority_reason": "string",          // one sentence justification
  "suggested_labels": [
    {"label": "string", "confidence": 0.0-1.0, "reason": "string"}
  ],                                    // labels ONLY from the allowed_labels list
  "questions": ["string"],             // ≤3 questions if needs_more_info=true, else []
  "repro_steps": ["string"] | null,    // numbered reproduction steps, or null if unavailable
  "needs_more_info": true|false
}
""").strip()


def _build_user_prompt(prompt: TriagePrompt) -> str:
    labels_str = ", ".join(f'"{lb}"' for lb in prompt.allowed_labels)
    return textwrap.dedent(f"""
Repository: {prompt.repo_full_name}
Allowed labels: [{labels_str}]

--- ISSUE TITLE ---
{prompt.title}

--- ISSUE BODY ---
{prompt.body_redacted or "(empty)"}

Analyse this issue and return the JSON triage report.
""").strip()


# ── Abstract interface ────────────────────────────────────────────────────────


class LLMClientBase(ABC):
    @abstractmethod
    def generate_triage(self, prompt: TriagePrompt) -> tuple[TriageOutput, dict[str, Any]]:
        """Return (TriageOutput, metadata) where metadata includes model/tokens."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return embedding vector for the given text."""


# ── Mock provider ─────────────────────────────────────────────────────────────


class MockLLMProvider(LLMClientBase):
    """Deterministic mock provider for development and testing."""

    MODEL = "mock-v1"

    def generate_triage(self, prompt: TriagePrompt) -> tuple[TriageOutput, dict[str, Any]]:
        # Heuristic priority based on title keywords
        title_lower = prompt.title.lower()
        if any(k in title_lower for k in ("crash", "data loss", "security", "critical")):
            priority = "P0"
        elif any(k in title_lower for k in ("error", "fail", "broken", "regression")):
            priority = "P1"
        elif any(k in title_lower for k in ("question", "how to", "how do", "docs", "documentation")):
            priority = "P3"
        else:
            priority = "P2"

        has_body = bool(prompt.body_redacted and prompt.body_redacted.strip())

        raw: dict[str, Any] = {
            "summary_bullets": [
                f"Issue reported in {prompt.repo_full_name}",
                f"Title: {prompt.title[:80]}",
                "Body provided: " + ("yes" if has_body else "no"),
                "Automated triage by Mock provider — replace with real LLM in production.",
            ],
            "priority": priority,
            "priority_reason": f"Keyword-based heuristic assigned {priority}.",
            "suggested_labels": [
                {
                    "label": lb,
                    "confidence": 0.85,
                    "reason": f"Mock: '{lb}' matches issue content.",
                }
                for lb in (prompt.allowed_labels[:2] if prompt.allowed_labels else [])
            ],
            "questions": (
                [
                    "What version of the software are you using?",
                    "Can you provide a minimal reproduction?",
                ]
                if not has_body
                else []
            ),
            "repro_steps": (
                ["1. Open the application", "2. Perform the action", "3. Observe the error"]
                if has_body
                else None
            ),
            "needs_more_info": not has_body,
        }

        output = TriageOutput(**raw)
        meta = {"model": self.MODEL, "tokens_in": 0, "tokens_out": 0}
        return output, meta

    def embed(self, text: str) -> list[float]:
        """Return a deterministic pseudo-embedding (zeros — for testing only)."""
        import hashlib

        h = hashlib.sha256(text.encode()).digest()
        # Create a 1536-dim vector with non-zero values from hash bytes
        seed = int.from_bytes(h, "big")
        import random

        rng = random.Random(seed)
        vec = [rng.gauss(0, 1) for _ in range(settings.embedding_dimensions)]
        # Normalize
        magnitude = sum(x**2 for x in vec) ** 0.5
        return [x / magnitude for x in vec]


# ── OpenAI-compatible provider ────────────────────────────────────────────────


class OpenAICompatibleProvider(LLMClientBase):
    """Calls any OpenAI-compatible chat completion API."""

    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("Install 'openai' package: pip install openai") from e

        self._client = OpenAI(  # type: ignore[assignment]
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
        self._model = settings.llm_model
        self._embedding_model = settings.embedding_model

    def generate_triage(self, prompt: TriagePrompt) -> tuple[TriageOutput, dict[str, Any]]:
        user_msg = _build_user_prompt(prompt)

        # First attempt
        raw_json, meta = self._call_completion(user_msg)
        result = self._parse_and_validate(raw_json)

        if result is None:
            # Retry with repair prompt
            logger.warning("LLM output failed validation; attempting JSON repair.")
            repair_msg = (
                f"The following JSON is malformed or invalid. "
                f"Fix it so it matches the required schema and return ONLY valid JSON:\n\n{raw_json}"
            )
            raw_json2, meta2 = self._call_completion(repair_msg)
            meta["tokens_in"] += meta2["tokens_in"]
            meta["tokens_out"] += meta2["tokens_out"]
            result = self._parse_and_validate(raw_json2)

        if result is None:
            raise ValueError(f"LLM output could not be parsed after repair. Raw: {raw_json[:500]}")

        return result, meta

    def _call_completion(self, user_content: str) -> tuple[str, dict[str, Any]]:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
        usage = response.usage
        meta = {
            "model": self._model,
            "tokens_in": usage.prompt_tokens if usage else 0,
            "tokens_out": usage.completion_tokens if usage else 0,
        }
        return content, meta

    def _parse_and_validate(self, raw: str) -> TriageOutput | None:
        try:
            data = json.loads(raw)
            return TriageOutput(**data)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.warning("LLM parse/validation error: %s", exc)
            return None

    def embed(self, text: str) -> list[float]:

        # Truncate to avoid token limit (8191 for text-embedding-3-small)
        truncated = text[:8000]
        response = self._client.embeddings.create(
            model=self._embedding_model,
            input=truncated,
        )
        return response.data[0].embedding


# ── Factory ───────────────────────────────────────────────────────────────────


def build_llm_client() -> LLMClientBase:
    """Instantiate the configured LLM provider."""
    if settings.llm_provider == "mock":
        logger.info("Using MockLLMProvider (set LLM_PROVIDER=openai for real calls)")
        return MockLLMProvider()
    elif settings.llm_provider == "openai":
        logger.info("Using OpenAICompatibleProvider model=%s", settings.llm_model)
        return OpenAICompatibleProvider()
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")


# Module-level singleton (lazy init in worker)
_client: LLMClientBase | None = None


def get_llm_client() -> LLMClientBase:
    global _client
    if _client is None:
        _client = build_llm_client()
    return _client
