"""GitHub webhook receiver.

POST /webhooks/github
- Verifies X-Hub-Signature-256 HMAC
- Ensures idempotency via X-GitHub-Delivery header
- Enqueues triage jobs for issues.opened events
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from redis import Redis
from rq import Queue
from sqlalchemy.orm import Session

from core.config import settings
from core.db.session import get_db
from core.models.db import DeliveryStatus, Issue, Repo, TriageStatus, WebhookDelivery

log = structlog.get_logger(__name__)
router = APIRouter(tags=["webhooks"])


# ── Redis / RQ setup ─────────────────────────────────────────────────────────


def _get_queue() -> Queue:
    redis_conn = Redis.from_url(settings.redis_url)
    return Queue(settings.worker_queue_name, connection=redis_conn)


# ── Signature verification ───────────────────────────────────────────────────


def _verify_signature(body: bytes, signature_header: str | None) -> None:
    """Raise 401 if the webhook signature is invalid."""
    if not settings.github_webhook_secret or settings.github_webhook_secret == "changeme":
        # Skip verification in dev/test mode
        log.warning("Webhook signature verification skipped (no secret configured)")
        return

    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")

    expected_sig = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_or_create_repo(db: Session, payload: dict[str, Any]) -> Repo:
    """Return existing Repo or create one from the webhook payload."""
    full_name: str = payload["repository"]["full_name"]
    installation_id: int | None = payload.get("installation", {}).get("id")

    repo = db.query(Repo).filter(Repo.full_name == full_name).first()
    if not repo:
        repo = Repo(
            full_name=full_name,
            github_installation_id=installation_id,
            config={},
        )
        db.add(repo)
        db.flush()
        log.info("Created new repo record", repo=full_name)
    elif installation_id and repo.github_installation_id != installation_id:
        repo.github_installation_id = installation_id
        db.flush()
    return repo


def _create_issue_from_payload(
    db: Session, repo: Repo, payload: dict[str, Any], delivery_id: str
) -> Issue:
    """Create or return existing Issue record from webhook payload."""
    gh_issue = payload["issue"]
    github_issue_id: int = gh_issue["id"]

    existing = (
        db.query(Issue)
        .filter(
            Issue.repo_id == repo.id,
            Issue.github_issue_id == github_issue_id,
        )
        .first()
    )
    if existing:
        return existing

    created_at_gh = None
    if gh_issue.get("created_at"):
        created_at_gh = datetime.fromisoformat(
            gh_issue["created_at"].replace("Z", "+00:00")
        )

    issue = Issue(
        repo_id=repo.id,
        github_issue_number=gh_issue["number"],
        github_issue_id=github_issue_id,
        title=gh_issue.get("title", ""),
        body=gh_issue.get("body") or "",
        author=gh_issue.get("user", {}).get("login", ""),
        state=gh_issue.get("state", "open"),
        created_at_github=created_at_gh,
        triage_status=TriageStatus.pending,
        last_delivery_id=delivery_id,
    )
    db.add(issue)
    db.flush()
    return issue


# ── Main webhook handler ──────────────────────────────────────────────────────


@router.post("/webhooks/github", summary="GitHub App webhook receiver")
async def github_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    body = await request.body()

    # 1. Verify signature
    _verify_signature(body, request.headers.get("X-Hub-Signature-256"))

    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")

    log.info("Received webhook", github_event=event, delivery_id=delivery_id)

    # 2. Idempotency check
    existing_delivery = db.get(WebhookDelivery, delivery_id)
    if existing_delivery:
        log.info("Duplicate delivery, skipping", delivery_id=delivery_id)
        return Response(content='{"status":"duplicate"}', media_type="application/json")

    payload: dict[str, Any] = json.loads(body)
    action: str = payload.get("action", "")

    # 3. Record delivery
    delivery = WebhookDelivery(
        delivery_id=delivery_id,
        event=event,
        action=action,
        payload=payload,
        status=DeliveryStatus.received,
    )
    db.add(delivery)
    db.flush()

    # 4. Only handle issues.opened
    if event != "issues" or action != "opened":
        delivery.status = DeliveryStatus.ignored
        log.info("Ignoring event", github_event=event, action=action)
        return Response(content='{"status":"ignored"}', media_type="application/json")

    try:
        # 5. Upsert repo + issue
        repo = _get_or_create_repo(db, payload)
        issue = _create_issue_from_payload(db, repo, payload, delivery_id)

        # 6. Enqueue triage job
        queue = _get_queue()
        queue.enqueue(
            "apps.worker.jobs.triage.triage_issue",
            str(issue.id),
            delivery_id,
            job_timeout=300,
            retry_count=2,
        )

        delivery.status = DeliveryStatus.enqueued
        log.info(
            "Enqueued triage job",
            issue_id=str(issue.id),
            repo=repo.full_name,
            issue_number=issue.github_issue_number,
        )

    except Exception as exc:
        log.exception("Failed to process webhook", error=str(exc))
        delivery.status = DeliveryStatus.failed
        delivery.error = str(exc)
        # Still return 200 to prevent GitHub retries for non-transient errors
        return Response(
            content='{"status":"error","detail":"internal processing error"}',
            media_type="application/json",
            status_code=200,
        )

    return Response(content='{"status":"enqueued"}', media_type="application/json")
