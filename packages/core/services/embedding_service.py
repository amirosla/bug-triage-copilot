"""Embedding storage and similarity search service."""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.models.db import Issue, IssueEmbedding

logger = logging.getLogger(__name__)

try:
    from pgvector.sqlalchemy import Vector  # noqa: F401

    _PGVECTOR = True
except ImportError:
    _PGVECTOR = False


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x**2 for x in a))
    mag_b = math.sqrt(sum(x**2 for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def save_embedding(db: Session, issue_id: uuid.UUID, embedding: list[float]) -> None:
    """Persist or update an issue embedding."""
    existing = db.get(IssueEmbedding, issue_id)
    if existing:
        existing.embedding = embedding
    else:
        db.add(IssueEmbedding(issue_id=issue_id, embedding=embedding))
    db.flush()


def find_similar_issues(
    db: Session,
    repo_id: uuid.UUID,
    issue_id: uuid.UUID,
    query_embedding: list[float],
    top_k: int = 5,
    threshold: float = 0.80,
) -> list[dict[str, Any]]:
    """Find similar issues in the same repo using cosine similarity.

    Uses pgvector's native operator when available, falls back to Python.
    """
    if _PGVECTOR:
        return _find_similar_pgvector(db, repo_id, issue_id, query_embedding, top_k, threshold)
    else:
        return _find_similar_python(db, repo_id, issue_id, query_embedding, top_k, threshold)


def _find_similar_pgvector(
    db: Session,
    repo_id: uuid.UUID,
    issue_id: uuid.UUID,
    query_embedding: list[float],
    top_k: int,
    threshold: float,
) -> list[dict[str, Any]]:
    """Use pgvector's <=> (cosine distance) operator for similarity search."""
    embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"

    sql = text("""
        SELECT
            i.id,
            i.github_issue_number,
            i.title,
            r.full_name,
            1 - (ie.embedding <=> :embedding::vector) AS score
        FROM issue_embeddings ie
        JOIN issues i ON ie.issue_id = i.id
        JOIN repos r ON i.repo_id = r.id
        WHERE i.repo_id = :repo_id
          AND i.id != :issue_id
          AND ie.embedding IS NOT NULL
          AND 1 - (ie.embedding <=> :embedding::vector) >= :threshold
        ORDER BY ie.embedding <=> :embedding::vector
        LIMIT :top_k
    """)

    rows = db.execute(
        sql,
        {
            "embedding": embedding_str,
            "repo_id": str(repo_id),
            "issue_id": str(issue_id),
            "threshold": threshold,
            "top_k": top_k,
        },
    ).fetchall()

    return [
        {
            "issue_number": row.github_issue_number,
            "title": row.title,
            "url": f"https://github.com/{row.full_name}/issues/{row.github_issue_number}",
            "score": round(float(row.score), 4),
        }
        for row in rows
    ]


def _find_similar_python(
    db: Session,
    repo_id: uuid.UUID,
    issue_id: uuid.UUID,
    query_embedding: list[float],
    top_k: int,
    threshold: float,
) -> list[dict[str, Any]]:
    """Python-level cosine similarity fallback when pgvector is unavailable."""
    rows = (
        db.query(IssueEmbedding)
        .join(IssueEmbedding.issue)
        .filter(
            Issue.repo_id == repo_id,
            Issue.id != issue_id,
            IssueEmbedding.embedding.isnot(None),
        )
        .all()
    )

    scored: list[tuple[float, IssueEmbedding]] = []
    for row in rows:
        emb = row.embedding
        if not emb:
            continue
        sim = _cosine_similarity(query_embedding, emb)
        if sim >= threshold:
            scored.append((sim, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, emb_row in scored[:top_k]:
        issue = emb_row.issue
        results.append(
            {
                "issue_number": issue.github_issue_number,
                "title": issue.title,
                "url": f"https://github.com/{issue.repo.full_name}/issues/{issue.github_issue_number}",
                "score": round(score, 4),
            }
        )
    return results
