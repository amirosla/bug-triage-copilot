"""Triage job: LLM analysis, embedding, similarity search, GitHub comment."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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


# ── Comment builders ──────────────────────────────────────────────────────────


def _render_header(triage: TriageOutput, marker: str, timestamp: str) -> list[str]:
    """Shared header for all comment types."""
    priority_emoji = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}.get(triage.priority, "⚪")
    type_emoji = {
        "bug": "🐛", "enhancement": "✨", "question": "❓",
        "docs": "📚", "chore": "🔧", "security": "🔒", "other": "📌",
    }.get(triage.issue_type or "other", "📌")

    lines: list[str] = [
        marker,
        "",
        "## 🤖 Bug Triage Copilot",
        "",
        f"**Type:** {type_emoji} `{triage.issue_type or 'other'}`"
        + (f" *(confidence: {int(triage.issue_type_confidence * 100)}%)*" if triage.issue_type_confidence else ""),
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
    return lines


def _render_labels(triage: TriageOutput) -> list[str]:
    lines: list[str] = []
    if triage.suggested_labels:
        lines += ["### 🏷️ Suggested Labels", ""]
        for lbl in triage.suggested_labels:
            confidence_bar = "█" * int(lbl.confidence * 10) + "░" * (10 - int(lbl.confidence * 10))
            lines.append(
                f"- **`{lbl.label}`** `{confidence_bar}` {int(lbl.confidence * 100)}%"
                + (f" — {lbl.reason}" if lbl.reason else "")
            )
        lines.append("")
    return lines


def _render_similar(similar: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    if similar:
        lines += ["### 🔗 Similar Issues", ""]
        for sim in similar:
            score_pct = int(sim["score"] * 100)
            title = sim.get("title", "")
            title_snippet = f": {title[:60]}…" if len(title) > 60 else (f": {title}" if title else "")
            lines.append(
                f"- #{sim['issue_number']}{title_snippet} ([view]({sim['url']})) — {score_pct}% similar"
            )
        lines.append("")
    return lines


def _render_questions(triage: TriageOutput) -> list[str]:
    lines: list[str] = []
    if triage.needs_more_info and triage.questions:
        lines += ["### ❓ Questions for Author", ""]
        for q in triage.questions:
            lines.append(f"- {q}")
        lines.append("")
    return lines


def _build_bug_comment_md(
    triage: TriageOutput,
    similar: list[dict[str, Any]],
    model: str | None,
    marker: str,
    timestamp: str,
) -> str:
    lines = _render_header(triage, marker, timestamp)
    lines += _render_labels(triage)

    if triage.repro_steps:
        lines += ["### 🔁 Reproduction Steps", ""]
        for i, step in enumerate(triage.repro_steps, 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    lines += _render_questions(triage)
    lines += _render_similar(similar)
    lines += [
        "---",
        f"*Analysed by Bug Triage Copilot · Model: `{model or 'unknown'}` · {timestamp}*",
    ]
    return "\n".join(lines)


def _build_enhancement_comment_md(
    triage: TriageOutput,
    similar: list[dict[str, Any]],
    model: str | None,
    marker: str,
    timestamp: str,
) -> str:
    lines = _render_header(triage, marker, timestamp)

    if triage.problem_statement:
        lines += ["### 🎯 Problem", "", triage.problem_statement, ""]

    if triage.acceptance_criteria:
        lines += ["### ✅ Acceptance Criteria", ""]
        for criterion in triage.acceptance_criteria:
            lines.append(f"- [ ] {criterion}")
        lines.append("")

    if triage.proposed_solution:
        lines += ["### 💡 Proposed Solution", ""]
        for i, step in enumerate(triage.proposed_solution, 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    lines += _render_questions(triage)
    lines += _render_labels(triage)
    lines += _render_similar(similar)
    lines += [
        "---",
        f"*Analysed by Bug Triage Copilot · Model: `{model or 'unknown'}` · {timestamp}*",
    ]
    return "\n".join(lines)


def _build_comment_md(
    triage: TriageOutput,
    similar: list[dict[str, Any]],
    model: str | None,
    issue_url: str,
    marker: str,
) -> str:
    """Render the Markdown comment posted to GitHub. Dispatches by issue_type."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if triage.issue_type == "enhancement":
        return _build_enhancement_comment_md(triage, similar, model, marker, timestamp)
    return _build_bug_comment_md(triage, similar, model, marker, timestamp)


# ── Main job function ─────────────────────────────────────────────────────────


def triage_issue(issue_id: str, delivery_id: str) -> None:
    """
    Full triage pipeline for a single GitHub Issue.

    Steps:
    1. Mark issue as processing
    2. Load repo config
    3. Redact secrets from body
    4. Call LLM for triage analysis
    5. Filter labels to allowed list
    6. Validate output (Pydantic)
    7. Generate embedding (input depends on issue_type)
    8. Find similar issues
    9. Post GitHub comment
    10. Optionally apply labels
    11. Mark issue as done
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
            allowed_labels_set = set(cfg["allowed_labels"])

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

            # ── Step 4: Filter labels to allowed list ────────────────────────
            filtered_labels = []
            for lbl in triage_output.suggested_labels:
                if lbl.label in allowed_labels_set:
                    filtered_labels.append(lbl)
                else:
                    log.warning(
                        "LLM suggested label not in allowed list, filtering out",
                        label=lbl.label,
                        allowed=list(allowed_labels_set),
                    )
            triage_output.suggested_labels = filtered_labels

            # ── Step 5: Persist triage result ────────────────────────────────
            summary_text = "\n".join(f"• {b}" for b in triage_output.summary_bullets)
            existing_result = issue.triage_result
            if existing_result:
                result_row = existing_result
            else:
                result_row = TriageResult(issue_id=issue.id)
                issue.triage_result = result_row

            result_row.summary_md = summary_text
            result_row.priority = triage_output.priority
            result_row.priority_reason = triage_output.priority_reason
            result_row.suggested_labels = [lbl.model_dump() for lbl in triage_output.suggested_labels]
            result_row.questions = triage_output.questions
            result_row.repro_steps = triage_output.repro_steps
            result_row.issue_type = triage_output.issue_type
            result_row.issue_type_confidence = triage_output.issue_type_confidence
            result_row.problem_statement = triage_output.problem_statement
            result_row.acceptance_criteria = triage_output.acceptance_criteria
            result_row.proposed_solution = triage_output.proposed_solution
            result_row.llm_model = llm_meta.get("model")
            result_row.tokens_in = llm_meta.get("tokens_in")
            result_row.tokens_out = llm_meta.get("tokens_out")
            db.flush()

            # ── Step 6: Embedding (input depends on issue_type) ──────────────
            if triage_output.issue_type == "enhancement":
                parts = [issue.title]
                if triage_output.problem_statement:
                    parts.append(triage_output.problem_statement)
                if triage_output.acceptance_criteria:
                    parts.append("\n".join(triage_output.acceptance_criteria))
                parts.append(summary_text)
                embed_text = "\n\n".join(parts)
            else:
                embed_text = f"{issue.title}\n\n{body_redacted}\n\n{summary_text}"

            try:
                embedding = llm.embed(embed_text)
                save_embedding(db, issue.id, embedding)
                db.flush()
            except Exception as emb_exc:
                log.warning("Embedding failed (non-fatal)", error=str(emb_exc))
                embedding = []

            # ── Step 7: Similar issues ───────────────────────────────────────
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

            # ── Step 8: Post GitHub comment ──────────────────────────────────
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

                    # ── Step 9: Apply labels (if enabled) ───────────────────
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

            # ── Step 10: Mark done ───────────────────────────────────────────
            issue.triage_status = TriageStatus.done

            delivery = db.get(WebhookDelivery, delivery_id)
            if delivery:
                delivery.status = DeliveryStatus.done
                delivery.processed = True
                delivery.processed_at = datetime.now(UTC)

            log.info(
                "Triage complete",
                issue_id=issue_id,
                issue_type=triage_output.issue_type,
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
