"""Unit tests for LLM output parsing and validation."""

from __future__ import annotations

import json

import pytest
from core.models.schemas import TriageOutput, TriagePrompt
from core.services.llm_client import MockLLMProvider
from pydantic import ValidationError


class TestTriageOutputSchema:
    """Tests for Pydantic schema validation of LLM output."""

    def _valid_payload(self, **overrides) -> dict:
        base = {
            "summary_bullets": ["The app crashes on upload", "Only affects files > 10MB"],
            "priority": "P1",
            "priority_reason": "Data loss risk for users uploading large files.",
            "suggested_labels": [
                {"label": "bug", "confidence": 0.95, "reason": "Clear crash report"},
                {"label": "regression", "confidence": 0.80, "reason": "Worked in 1.3.x"},
            ],
            "questions": [],
            "repro_steps": ["Upload a file > 10MB", "Observe MemoryError"],
            "needs_more_info": False,
        }
        base.update(overrides)
        return base

    def test_valid_payload_parses(self):
        output = TriageOutput(**self._valid_payload())
        assert output.priority == "P1"
        assert len(output.summary_bullets) == 2
        assert len(output.suggested_labels) == 2

    def test_invalid_priority_raises(self):
        with pytest.raises(ValidationError):
            TriageOutput(**self._valid_payload(priority="LOW"))

    def test_empty_summary_raises(self):
        with pytest.raises(ValidationError):
            TriageOutput(**self._valid_payload(summary_bullets=[]))

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            TriageOutput(
                **self._valid_payload(
                    suggested_labels=[{"label": "bug", "confidence": 1.5, "reason": "x"}]
                )
            )

    def test_questions_cleared_when_not_needs_more_info(self):
        output = TriageOutput(
            **self._valid_payload(
                needs_more_info=False,
                questions=["What version?", "Can you reproduce?"],
            )
        )
        # Validator should clear questions when needs_more_info=False
        assert output.questions == []

    def test_repro_steps_can_be_none(self):
        output = TriageOutput(**self._valid_payload(repro_steps=None))
        assert output.repro_steps is None

    def test_all_priority_levels_valid(self):
        for p in ("P0", "P1", "P2", "P3"):
            output = TriageOutput(**self._valid_payload(priority=p))
            assert output.priority == p

    def test_json_round_trip(self):
        payload = self._valid_payload()
        output = TriageOutput(**payload)
        json_str = output.model_dump_json()
        restored = TriageOutput(**json.loads(json_str))
        assert restored.priority == output.priority
        assert restored.summary_bullets == output.summary_bullets


class TestMockLLMProvider:
    """Tests for the mock LLM provider."""

    def setup_method(self):
        self.provider = MockLLMProvider()

    def _make_prompt(self, title: str, body: str = "", labels: list[str] | None = None) -> TriagePrompt:
        return TriagePrompt(
            title=title,
            body_redacted=body,
            allowed_labels=labels or ["bug", "enhancement", "question", "documentation"],
            repo_full_name="acme/myapp",
        )

    def test_returns_triage_output(self):
        prompt = self._make_prompt("App crashes on upload")
        output, meta = self.provider.generate_triage(prompt)
        assert isinstance(output, TriageOutput)
        assert meta["model"] == "mock-v1"

    def test_critical_keywords_yield_p0(self):
        prompt = self._make_prompt("Critical security vulnerability in auth")
        output, _ = self.provider.generate_triage(prompt)
        assert output.priority == "P0"

    def test_error_keywords_yield_p1(self):
        prompt = self._make_prompt("Login error after update")
        output, _ = self.provider.generate_triage(prompt)
        assert output.priority == "P1"

    def test_question_keywords_yield_p3(self):
        prompt = self._make_prompt("How do I configure the timeout?")
        output, _ = self.provider.generate_triage(prompt)
        assert output.priority == "P3"

    def test_empty_body_needs_more_info(self):
        prompt = self._make_prompt("Something is broken", body="")
        output, _ = self.provider.generate_triage(prompt)
        assert output.needs_more_info is True
        assert len(output.questions) > 0

    def test_body_present_no_needs_more_info(self):
        prompt = self._make_prompt("Bug report", body="Steps: 1. Do X 2. See error Y")
        output, _ = self.provider.generate_triage(prompt)
        assert output.needs_more_info is False

    def test_suggested_labels_from_allowed_list(self):
        allowed = ["bug", "security", "documentation"]
        prompt = self._make_prompt("title", labels=allowed)
        output, _ = self.provider.generate_triage(prompt)
        for lbl in output.suggested_labels:
            assert lbl.label in allowed

    def test_embed_returns_correct_dimensions(self):
        vec = self.provider.embed("Some issue text")
        assert len(vec) == 1536  # default embedding_dimensions

    def test_embed_is_normalized(self):
        import math
        vec = self.provider.embed("test")
        magnitude = math.sqrt(sum(x**2 for x in vec))
        assert abs(magnitude - 1.0) < 1e-6

    def test_embed_same_text_same_vector(self):
        v1 = self.provider.embed("reproducible text")
        v2 = self.provider.embed("reproducible text")
        assert v1 == v2

    def test_embed_different_text_different_vector(self):
        v1 = self.provider.embed("text one")
        v2 = self.provider.embed("text two")
        assert v1 != v2
