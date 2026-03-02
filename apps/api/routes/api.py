"""REST API routes for issues, repos, and triage results."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from core.db.session import get_db
from core.models.db import Issue, Repo, TriageStatus, WebhookDelivery
from core.models.schemas import (
    IssueOut,
    PaginatedIssues,
    RepoConfigPatch,
    RepoOut,
    WebhookDeliveryOut,
)
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, selectinload

log = structlog.get_logger(__name__)
router = APIRouter()


# ── Repos ────────────────────────────────────────────────────────────────────


@router.get("/repos", response_model=list[RepoOut], summary="List all registered repos")
def list_repos(db: Session = Depends(get_db)) -> list[Repo]:
    return db.query(Repo).order_by(Repo.full_name).all()  # type: ignore[return-value]


@router.get("/repos/{repo_id}", response_model=RepoOut, summary="Get repo by ID")
def get_repo(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> Repo:
    repo = db.get(Repo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")
    return repo  # type: ignore[return-value]


@router.patch("/repos/{repo_id}/config", response_model=RepoOut, summary="Update repo triage config")
def update_repo_config(
    repo_id: uuid.UUID,
    patch: RepoConfigPatch,
    db: Session = Depends(get_db),
) -> Repo:
    repo = db.get(Repo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    config: dict[str, Any] = dict(repo.config or {})
    if patch.allowed_labels is not None:
        config["allowed_labels"] = patch.allowed_labels
    if patch.auto_apply_labels is not None:
        config["auto_apply_labels"] = patch.auto_apply_labels
    if patch.label_confidence_threshold is not None:
        config["label_confidence_threshold"] = patch.label_confidence_threshold
    if patch.similarity_threshold is not None:
        config["similarity_threshold"] = patch.similarity_threshold

    repo.config = config
    db.flush()
    log.info("Updated repo config", repo=repo.full_name)
    return repo  # type: ignore[return-value]


# ── Issues ────────────────────────────────────────────────────────────────────


@router.get("/issues", response_model=PaginatedIssues, summary="List issues with optional filters")
def list_issues(
    repo: str | None = Query(default=None, description="Filter by repo full_name (owner/repo)"),
    status: str | None = Query(
        default=None,
        description=f"Filter by triage_status: {', '.join(TriageStatus.ALL)}",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = db.query(Issue).options(
        selectinload(Issue.repo),
        selectinload(Issue.triage_result),
    )

    if repo:
        query = query.join(Issue.repo).filter(Repo.full_name == repo)
    if status:
        if status not in TriageStatus.ALL:
            raise HTTPException(status_code=422, detail=f"Invalid status: {status!r}")
        query = query.filter(Issue.triage_status == status)

    total = query.count()
    items = (
        query.order_by(Issue.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/issues/{issue_id}", response_model=IssueOut, summary="Get issue with triage result")
def get_issue(issue_id: uuid.UUID, db: Session = Depends(get_db)) -> Issue:
    issue = (
        db.query(Issue)
        .options(
            selectinload(Issue.repo),
            selectinload(Issue.triage_result),
        )
        .filter(Issue.id == issue_id)
        .first()
    )
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue  # type: ignore[return-value]


# ── Webhook deliveries ────────────────────────────────────────────────────────


@router.get(
    "/deliveries",
    response_model=list[WebhookDeliveryOut],
    summary="List recent webhook deliveries",
)
def list_deliveries(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[WebhookDelivery]:
    return (
        db.query(WebhookDelivery)
        .order_by(WebhookDelivery.received_at.desc())
        .limit(limit)
        .all()
    )  # type: ignore[return-value]
