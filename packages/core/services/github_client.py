"""GitHub App client with JWT auth, installation tokens, and retry/backoff."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from core.config import settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
TOKEN_TTL_SECONDS = 50 * 60  # refresh 10 min before expiry


def _is_retryable(exc: BaseException) -> bool:
    """Retry on rate-limit (429) and server errors (5xx)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.ConnectError, httpx.TimeoutException))


class GitHubAppAuth:
    """Manages GitHub App JWT and installation access tokens."""

    def __init__(self) -> None:
        self._app_id = settings.github_app_id
        self._private_key = settings.github_private_key
        self._token_cache: dict[int, tuple[str, float]] = {}  # installation_id -> (token, expiry)

    def _generate_jwt(self) -> str:
        import jwt  # PyJWT

        now = int(time.time())
        payload = {
            "iat": now - 60,  # issued 60s ago to allow clock skew
            "exp": now + 9 * 60,  # 9 min (max 10)
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def get_installation_token(self, installation_id: int) -> str:
        """Return a valid installation access token, refreshing if needed."""
        cached = self._token_cache.get(installation_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        jwt_token = self._generate_jwt()
        url = f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"

        with httpx.Client(timeout=15) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {jwt_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()

        data = resp.json()
        token = data["token"]
        # expires_at is ISO8601; cache until TOKEN_TTL_SECONDS from now
        expiry = time.time() + TOKEN_TTL_SECONDS
        self._token_cache[installation_id] = (token, expiry)
        return token


class GitHubClient:
    """High-level GitHub API client for issue interactions."""

    def __init__(self, installation_id: int | None = None) -> None:
        self._auth = GitHubAppAuth()
        self._installation_id = installation_id
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_token(self) -> str:
        if not self._installation_id:
            raise ValueError("installation_id is required for authenticated requests")
        return self._auth.get_installation_token(self._installation_id)

    def _auth_headers(self) -> dict[str, str]:
        return {**self._headers, "Authorization": f"Bearer {self._get_token()}"}

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def post_issue_comment(self, owner: str, repo: str, issue_number: int, body_md: str) -> dict[str, Any]:
        """Post a new comment on an issue."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        with httpx.Client(timeout=20) as client:
            resp = client.post(url, json={"body": body_md}, headers=self._auth_headers())
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            logger.info(
                "Posted comment on %s/%s#%d  comment_id=%s",
                owner, repo, issue_number, data.get("id"),
            )
            return data

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def update_issue_comment(
        self, owner: str, repo: str, comment_id: int, body_md: str
    ) -> dict[str, Any]:
        """Update an existing comment."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/comments/{comment_id}"
        with httpx.Client(timeout=20) as client:
            resp = client.patch(url, json={"body": body_md}, headers=self._auth_headers())
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def list_issue_comments(self, owner: str, repo: str, issue_number: int) -> list[dict[str, Any]]:
        """List all comments on an issue."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        with httpx.Client(timeout=20) as client:
            resp = client.get(url, headers=self._auth_headers())
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def add_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> None:
        """Add labels to an issue."""
        if not labels:
            return
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/labels"
        with httpx.Client(timeout=20) as client:
            resp = client.post(url, json={"labels": labels}, headers=self._auth_headers())
            resp.raise_for_status()
            logger.info(
                "Applied labels %s to %s/%s#%d", labels, owner, repo, issue_number
            )

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def get_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        """Fetch a single issue."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}"
        with httpx.Client(timeout=20) as client:
            resp = client.get(url, headers=self._auth_headers())
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    def find_existing_bot_comment(
        self, owner: str, repo: str, issue_number: int, marker: str
    ) -> int | None:
        """Return comment_id of existing bot comment, or None."""
        try:
            comments = self.list_issue_comments(owner, repo, issue_number)
            for comment in comments:
                if marker in (comment.get("body") or ""):
                    return comment["id"]
        except Exception as exc:
            logger.warning("Could not list comments: %s", exc)
        return None

    def upsert_triage_comment(
        self, owner: str, repo: str, issue_number: int, body_md: str, marker: str
    ) -> None:
        """Post or update the triage comment."""
        existing_id = self.find_existing_bot_comment(owner, repo, issue_number, marker)
        if existing_id:
            self.update_issue_comment(owner, repo, existing_id, body_md)
            logger.info("Updated existing bot comment %d", existing_id)
        else:
            self.post_issue_comment(owner, repo, issue_number, body_md)
