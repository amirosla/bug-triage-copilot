"""Pydantic v2 schemas for API I/O and LLM output validation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ── LLM triage output ────────────────────────────────────────────────────────


class SuggestedLabel(BaseModel):
    label: str = Field(..., description="Label name from the allowed list.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score 0-1.")
    reason: str = Field(..., description="One-sentence justification.")


class TriageOutput(BaseModel):
    """Strict schema for LLM JSON output. Validated before DB write."""

    summary_bullets: list[str] = Field(
        ..., min_length=1, max_length=8, description="3-6 concise bullet points."
    )
    priority: str = Field(..., pattern=r"^P[0-3]$", description="P0 (critical) to P3 (low).")
    priority_reason: str = Field(..., description="One sentence explaining the priority.")
    suggested_labels: list[SuggestedLabel] = Field(
        ..., max_length=5, description="Labels from the allowed list with confidence."
    )
    questions: list[str] = Field(
        default_factory=list, max_length=3, description="Clarifying questions for the author."
    )
    repro_steps: list[str] | None = Field(
        default=None, description="Reproduction steps if available."
    )
    needs_more_info: bool = Field(
        default=False, description="True if the issue lacks sufficient information."
    )

    @field_validator("summary_bullets")
    @classmethod
    def bullets_not_empty(cls, v: list[str]) -> list[str]:
        if any(not b.strip() for b in v):
            raise ValueError("summary_bullets must not contain empty strings")
        return v

    @model_validator(mode="after")
    def questions_only_when_needed(self) -> "TriageOutput":
        if not self.needs_more_info and self.questions:
            # Silently clear questions when needs_more_info is False
            self.questions = []
        return self


class TriagePrompt(BaseModel):
    """Input to the LLM triage call."""

    title: str
    body_redacted: str
    allowed_labels: list[str]
    repo_full_name: str


# ── API request/response schemas ─────────────────────────────────────────────


class RepoConfigPatch(BaseModel):
    allowed_labels: list[str] | None = None
    auto_apply_labels: bool | None = None
    label_confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class RepoOut(BaseModel):
    id: uuid.UUID
    full_name: str
    github_installation_id: int | None
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TriageResultOut(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    summary_md: str
    priority: str
    priority_reason: str
    suggested_labels: list[dict[str, Any]]
    questions: list[str]
    repro_steps: list[str] | None
    similar_issues: list[dict[str, Any]]
    llm_model: str | None
    tokens_in: int | None
    tokens_out: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class IssueOut(BaseModel):
    id: uuid.UUID
    repo_id: uuid.UUID
    github_issue_number: int
    github_issue_id: int
    title: str
    body: str
    author: str
    state: str
    triage_status: str
    created_at_github: datetime | None
    created_at: datetime
    updated_at: datetime
    triage_result: TriageResultOut | None = None

    model_config = {"from_attributes": True}


class WebhookDeliveryOut(BaseModel):
    delivery_id: str
    event: str
    action: str | None
    status: str
    received_at: datetime
    processed_at: datetime | None
    error: str | None

    model_config = {"from_attributes": True}


# ── Pagination ────────────────────────────────────────────────────────────────


class PaginatedIssues(BaseModel):
    items: list[IssueOut]
    total: int
    page: int
    page_size: int
