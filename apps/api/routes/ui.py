"""Server-side rendered UI routes (Jinja2)."""

from __future__ import annotations

import uuid
from pathlib import Path

from core.db.session import get_db
from core.models.db import Issue, Repo, TriageStatus
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, selectinload

router = APIRouter(tags=["ui"])

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse, summary="Dashboard — latest issues")
def dashboard(
    request: Request,
    repo: str | None = None,
    status: str | None = None,
    page: int = 1,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_size = 20
    query = db.query(Issue).options(
        selectinload(Issue.repo),
        selectinload(Issue.triage_result),
    )

    if repo:
        query = query.join(Issue.repo).filter(Repo.full_name == repo)
    if status and status in TriageStatus.ALL:
        query = query.filter(Issue.triage_status == status)

    total = query.count()
    issues = (
        query.order_by(Issue.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    repos = db.query(Repo).order_by(Repo.full_name).all()
    total_pages = max(1, (total + page_size - 1) // page_size)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "issues": issues,
            "repos": repos,
            "selected_repo": repo,
            "selected_status": status,
            "statuses": TriageStatus.ALL,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


@router.get("/issues/{issue_id}", response_class=HTMLResponse, summary="Issue detail view")
def issue_detail(
    request: Request,
    issue_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
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

    return templates.TemplateResponse(
        "issue_detail.html",
        {"request": request, "issue": issue},
    )


@router.get("/repos/{repo_id}/config", response_class=HTMLResponse, summary="Repo config editor")
def repo_config(
    request: Request,
    repo_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    repo = db.get(Repo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    return templates.TemplateResponse(
        "repo_config.html",
        {"request": request, "repo": repo, "config": repo.effective_config()},
    )
