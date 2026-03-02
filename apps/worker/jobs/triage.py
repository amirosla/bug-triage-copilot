"""Triage job: LLM analysis, embedding, similarity search, GitHub comment."""

from __future__ import annotations

import textwrap
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from core.config import settings
from core.db.session import get_session
from core.models.db import DeliveryStatus, Issue, TriageResult, TriageStatus, WebhookDelivery
from core.models.schemas import TriageOutput, TriagePrompt
from core.services.embedding_service import find_similar_issues, save_embedding
from core.services.llm_client import get_llm_client
from core.services.secret_redaction import redact

log = structlog.get_logger(__name__)


# ── Comment builder ───────────────────────────────────────────────────────────


def _build_comment_md(
    triage: TriageOutput,
    similar: list[dict[str, Any]],
    model: str | None,
    issue_url: str,
    marker: str,
) -> str:
    """Render the Markdown comment posted to GitHub."""
    priority_emoji = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}.get(triage.priority, "⚪")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = [
        marker,
        "",
        "## 🤖 Bug Triage Copilot",
        "",
        "### 📋 Summary",
        "",
    ]
    for bullet in triage.summary_bullets:
        lines.append(f"- {bullet}")

    lines += [
        "",
        f"### {priority_emoji} Priority: **{triage.priority}**",
        "",
        f"> {triage.priority_reason}",
        "",
    ]

    if triage.suggested_labels:
        lines += ["### 🏷️ Suggested Labels", ""]
        for lbl in triage.suggested_labels:
            confidence_bar = "█" * int(lbl.confidence * 10) + "░" * (10 - int(lbl.confidence * 10))
            lines.append(
                f"- **`{lbl.label}`** `{confidence_bar}` {int(lbl.confidence * 100)}%"
                + (f" — {lbl.reason}" if lbl.reason else "")
            )
        lines.append("")

    if triage.repro_steps:
        lines += ["### 🔁 Reproduction Steps", ""]
        for i, step in enumerate(triage.repro_steps, 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    if triage.needs_more_info and triage.questions:
        lines += ["### ❓ Questions for Author", ""]
        for q in triage.questions:
            lines.append(f"- {q}")
        lines.append("")

    if similar:
        lines += ["### 🔗 Similar Issues", ""]
        for sim in similar:
            score_pct = int(sim["score"] * 100)
            title = sim.get("title", "")
            title_snippet = f": {title[:60]}…" if len(title) > 60 else (f": {title}" if title else "")
            lines.append(f"- #{sim['issue_number']}{title_snippet} ([view]({sim['url']})) — {score_pct}% similar")
        lines.append("")

    lines += [
        "---",
        f"*Analysed by Bug Triage Copilot · Model: `{model or 'unknown'}` · {timestamp}*",
    ]

    return "\n".join(lines)


# ── Main job function ─────────────────────────────────────────────────────────


def triage_issue(issue_id: str, delivery_id: str) -> None:
    """
    Full triage pipeline for a single GitHub Issue.

    Steps:
    1. Mark issue as processing
    2. Load repo config
    3. Redact secrets from body
    4. Call LLM for triage analysis
    5. Validate output (Pydantic)
    6. Generate embedding
    7. Find similar issues
    8. Post GitHub comment
    9. Optionally apply labels
    10. Mark issue as done
    """
    log.info("Starting triage job", issue_id=issue_id, delivery_id=delivery_id)

    with get_session() as db:
        issue: Issue | None = (
            db.query(Issue)
            .filter(Issue.id == uuid.UUID(issue_id))
            .first()
        )

        if not issue:
            log.error("Issue not found in DB", issue_id=issue_id)
            _mark_delivery_failed(delivery_id, "Issue not found")
            return

        # ── Step 1: Mark processing ──────────────────────────────────────────
        issue.triage_status = TriageStatus.processing
        db.flush()
        log.info(
            "Processing issue",
            repo=issue.repo.full_name,
            issue_number=issue.github_issue_number,
        )

        try:
            repo = issue.repo
            cfg = repo.effective_config()

            # ── Step 2: Redact secrets ───────────────────────────────────────
            body_redacted = redact(issue.body)

            # ── Step 3: LLM triage ───────────────────────────────────────────
            llm = get_llm_client()
            prompt = TriagePrompt(
                title=issue.title,
                body_redacted=body_redacted,
                allowed_labels=cfg["allowed_labels"],
                repo_full_name=repo.full_name,
            )
            triage_output, llm_meta = llm.generate_triage(prompt)

            # ── Step 4: Persist triage result ────────────────────────────────
            summary_text = "\n".join(f"• {b}" for b in triage_output.summary_bullets)
            existing_result = issue.triage_result
            if existing_result:
                result_row = existing_result
            else:
                result_row = TriageResult(issue_id=issue.id)
                # Assign via relationship so the Python object is updated immediately
                issue.triage_result = result_row

            result_row.summary_md = summary_text
            result_row.priority = triage_output.priority
            result_row.priority_reason = triage_output.priority_reason
            result_row.suggested_labels = [
                lbl.model_dump() for lbl in triage_output.suggested_labels
            ]
            result_row.questions = triage_output.questions
            result_row.repro_steps = triage_output.repro_steps
            result_row.llm_model = llm_meta.get("model")
            result_row.tokens_in = llm_meta.get("tokens_in")
            result_row.tokens_out = llm_meta.get("tokens_out")
            db.flush()

            # ── Step 5: Embedding ────────────────────────────────────────────
            embed_text = f"{issue.title}\n\n{issue.body}\n\n{summary_text}"
            try:
                embedding = llm.embed(embed_text)
                save_embedding(db, issue.id, embedding)
                db.flush()
            except Exception as emb_exc:
                log.warning("Embedding failed (non-fatal)", error=str(emb_exc))
                embedding = []

            # ── Step 6: Similar issues ───────────────────────────────────────
            similar: list[dict[str, Any]] = []
            if embedding:
                try:
                    similar = find_similar_issues(
                        db=db,
                        repo_id=repo.id,
                        issue_id=issue.id,
                        query_embedding=embedding,
                        top_k=5,
                        threshold=cfg["similarity_threshold"],
                    )
                    result_row.similar_issues = similar
                    db.flush()
                except Exception as sim_exc:
                    log.warning("Similarity search failed (non-fatal)", error=str(sim_exc))

            # ── Step 7: Post GitHub comment ──────────────────────────────────
            marker = settings.bot_comment_marker
            comment_md = _build_comment_md(
                triage=triage_output,
                similar=similar,
                model=llm_meta.get("model"),
                issue_url=issue.github_url,
                marker=marker,
            )

            if repo.github_installation_id and settings.github_app_id:
                try:
                    from core.services.github_client import GitHubClient

                    gh = GitHubClient(installation_id=repo.github_installation_id)
                    owner, repo_name = repo.full_name.split("/", 1)
                    gh.upsert_triage_comment(
                        owner=owner,
                        repo=repo_name,
                        issue_number=issue.github_issue_number,
                        body_md=comment_md,
                        marker=marker,
                    )

                    # ── Step 8: Apply labels (if enabled) ───────────────────
                    if cfg["auto_apply_labels"]:
                        threshold = cfg["label_confidence_threshold"]
                        labels_to_apply = [
                            lbl["label"]
                            for lbl in result_row.suggested_labels
                            if lbl.get("confidence", 0) >= threshold
                        ]
                        if labels_to_apply:
                            gh.add_labels(owner, repo_name, issue.github_issue_number, labels_to_apply)
                except Exception as gh_exc:
                    log.warning("GitHub API call failed (non-fatal)", error=str(gh_exc))
            else:
                log.info(
                    "Skipping GitHub comment (no installation ID or App ID)",
                    comment_preview=comment_md[:200],
                )

            # ── Step 9: Mark done ────────────────────────────────────────────
            issue.triage_status = TriageStatus.done

            # Mark delivery done
            delivery = db.get(WebhookDelivery, delivery_id)
            if delivery:
                delivery.status = DeliveryStatus.done
                delivery.processed = True
                delivery.processed_at = datetime.now(timezone.utc)

            log.info(
                "Triage complete",
                issue_id=issue_id,
                priority=triage_output.priority,
                labels=[lbl["label"] for lbl in result_row.suggested_labels],
                similar_count=len(similar),
            )

        except Exception as exc:
            log.exception("Triage job failed", issue_id=issue_id, error=str(exc))
            issue.triage_status = TriageStatus.failed

            delivery = db.get(WebhookDelivery, delivery_id)
            if delivery:
                delivery.status = DeliveryStatus.failed
                delivery.error = str(exc)[:1000]
            raise  # Re-raise so RQ marks the job as failed


def _mark_delivery_failed(delivery_id: str, error: str) -> None:
    """Mark a delivery as failed without a DB session from the caller."""
    try:
        with get_session() as db:
            delivery = db.get(WebhookDelivery, delivery_id)
            if delivery:
                delivery.status = DeliveryStatus.failed
                delivery.error = error
    except Exception:
        pass  # Best-effort
