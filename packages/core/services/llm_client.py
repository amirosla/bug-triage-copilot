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
- First classify the issue_type, then generate fields appropriate for that type.
- If the issue lacks information, set needs_more_info=true and add up to 3 clarifying questions.
- Do NOT repeat or expose any content marked as ***REDACTED***.
- Output ONLY a valid JSON object matching the schema below — no markdown fences, no prose.
- suggested_labels MUST only contain labels from the provided allowed_labels list.

issue_type classification guide:
  bug        — something is broken or not working as expected
  enhancement — new feature or improvement request
  question   — user asking how to do something
  docs       — documentation improvement request
  chore      — maintenance, refactoring, dependency update
  security   — security vulnerability or concern
  other      — doesn't fit any of the above

Output schema:
{
  "issue_type": "bug|enhancement|question|docs|chore|security|other",
  "issue_type_confidence": 0.0-1.0,
  "summary_bullets": ["string"],        // 3-6 concise bullets summarising the issue
  "priority": "P0|P1|P2|P3",           // P0=critical/data-loss, P1=major, P2=normal, P3=minor
  "priority_reason": "string",          // one sentence justification
  "suggested_labels": [
    {"label": "string", "confidence": 0.0-1.0, "reason": "string"}
  ],                                    // labels ONLY from the allowed_labels list
  "questions": ["string"],             // ≤3 questions if needs_more_info=true, else []
  "needs_more_info": true|false,
  "repro_steps": ["string"] | null,    // for bugs: numbered reproduction steps; for enhancements: null
  "problem_statement": "string" | null, // REQUIRED for enhancement: 1-2 sentence problem description
  "acceptance_criteria": ["string"] | null, // REQUIRED for enhancement: min 3 items (definition of done)
  "proposed_solution": ["string"] | null    // optional for enhancement: proposed solution steps
}

Field requirements by issue_type:
  bug        — repro_steps should be provided when available; problem_statement/acceptance_criteria can be null
  enhancement — problem_statement MUST be non-empty; acceptance_criteria MUST have ≥3 items; repro_steps=null

Examples:

Bug example:
Input title: "App crashes when uploading files larger than 10MB"
Output: {"issue_type": "bug", "issue_type_confidence": 0.95, "summary_bullets": ["File upload crashes on files > 10MB", "MemoryError observed in server logs"], "priority": "P1", "priority_reason": "Production crash affects all users uploading large files.", "suggested_labels": [{"label": "bug", "confidence": 0.95, "reason": "Clear crash report"}], "questions": [], "needs_more_info": false, "repro_steps": ["Open the app", "Upload a file larger than 10MB", "Observe MemoryError crash"], "problem_statement": null, "acceptance_criteria": null, "proposed_solution": null}

Enhancement example:
Input title: "Feature request: add dark mode support to the dashboard"
Output: {"issue_type": "enhancement", "issue_type_confidence": 0.92, "summary_bullets": ["User requests dark mode for the dashboard", "Preference should persist across sessions"], "priority": "P3", "priority_reason": "Nice-to-have UX improvement with no production impact.", "suggested_labels": [{"label": "enhancement", "confidence": 0.90, "reason": "New feature request"}], "questions": [], "needs_more_info": false, "repro_steps": null, "problem_statement": "Users who work in low-light environments cannot use a dark theme, causing eye strain.", "acceptance_criteria": ["A toggle to switch between light and dark mode is visible in the UI", "The selected theme persists in localStorage across page reloads", "All dashboard pages and components support the dark theme"], "proposed_solution": ["Add a theme toggle button to the header", "Store the preference in localStorage", "Apply a CSS class to the root element to switch themes"]}
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

_ENHANCEMENT_KEYWORDS = ("feature", "add ", "implement", "request", "improve", "enhance", "support for", "would be nice", "wish")
_BUG_KEYWORDS = ("crash", "data loss", "security", "critical", "error", "fail", "broken", "regression")
_QUESTION_KEYWORDS = ("question", "how to", "how do", "docs", "documentation")


class MockLLMProvider(LLMClientBase):
    """Deterministic mock provider for development and testing."""

    MODEL = "mock-v1"

    def generate_triage(self, prompt: TriagePrompt) -> tuple[TriageOutput, dict[str, Any]]:
        title_lower = prompt.title.lower()
        has_body = bool(prompt.body_redacted and prompt.body_redacted.strip())

        # Classify issue type
        if any(k in title_lower for k in ("security", "vulnerability", "cve")):
            issue_type = "security"
        elif any(k in title_lower for k in _ENHANCEMENT_KEYWORDS):
            issue_type = "enhancement"
        elif any(k in title_lower for k in _QUESTION_KEYWORDS):
            issue_type = "question"
        elif any(k in title_lower for k in ("docs", "documentation", "readme")):
            issue_type = "docs"
        else:
            issue_type = "bug"

        # Heuristic priority based on title keywords
        if any(k in title_lower for k in ("crash", "data loss", "security", "critical")):
            priority = "P0"
        elif any(k in title_lower for k in ("error", "fail", "broken", "regression")):
            priority = "P1"
        elif any(k in title_lower for k in ("question", "how to", "how do", "docs", "documentation")):
            priority = "P3"
        elif issue_type == "enhancement":
            priority = "P3"
        else:
            priority = "P2"

        raw: dict[str, Any] = {
            "issue_type": issue_type,
            "issue_type_confidence": 0.85,
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
            "needs_more_info": not has_body,
            "repro_steps": None,
            "problem_statement": None,
            "acceptance_criteria": None,
            "proposed_solution": None,
        }

        if issue_type == "enhancement":
            raw["repro_steps"] = None
            raw["problem_statement"] = f"Users need: {prompt.title[:100]}"
            raw["acceptance_criteria"] = [
                "The feature is implemented and accessible to users",
                "The feature works as described in the issue",
                "Tests are added for the new functionality",
                "Documentation is updated if needed",
            ]
            if has_body:
                raw["proposed_solution"] = [
                    "Review the issue requirements",
                    "Implement the requested feature",
                    "Add tests and update documentation",
                ]
        elif issue_type == "bug":
            raw["repro_steps"] = (
                ["1. Open the application", "2. Perform the action", "3. Observe the error"]
                if has_body
                else None
            )

        output = TriageOutput(**raw)
        meta = {"model": self.MODEL, "tokens_in": 0, "tokens_out": 0}
        return output, meta

    def embed(self, text: str) -> list[float]:
        """Return a deterministic pseudo-embedding (zeros — for testing only)."""
        import hashlib
        import random

        h = hashlib.sha256(text.encode()).digest()
        seed = int.from_bytes(h, "big")
        rng = random.Random(seed)
        vec = [rng.gauss(0, 1) for _ in range(settings.embedding_dimensions)]
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
        result, error = self._parse_and_validate(raw_json)

        if result is None:
            # Retry with repair prompt including the specific error
            logger.warning("LLM output failed validation; attempting JSON repair. Error: %s", error)
            repair_msg = (
                f"The following JSON is invalid or does not match the required schema.\n"
                f"Validation error: {error}\n\n"
                f"Fix it and return ONLY valid JSON:\n\n{raw_json}"
            )
            raw_json2, meta2 = self._call_completion(repair_msg)
            meta["tokens_in"] += meta2["tokens_in"]
            meta["tokens_out"] += meta2["tokens_out"]
            result, _ = self._parse_and_validate(raw_json2)

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

    def _parse_and_validate(self, raw: str) -> tuple[TriageOutput | None, str | None]:
        try:
            data = json.loads(raw)
            return TriageOutput(**data), None
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.warning("LLM parse/validation error: %s", exc)
            return None, str(exc)

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
