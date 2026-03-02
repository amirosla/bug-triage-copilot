"""Unit tests for webhook signature verification and payload parsing."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


class TestWebhookSignatureVerification:
    """Tests for HMAC-SHA256 webhook signature verification."""

    def _make_signature(self, body: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_valid_signature_passes(self):
        from apps.api.routes.webhooks import _verify_signature

        body = b'{"action": "opened"}'
        secret = "my-webhook-secret"

        with patch("apps.api.routes.webhooks.settings") as mock_settings:
            mock_settings.github_webhook_secret = secret
            sig = self._make_signature(body, secret)
            # Should not raise
            _verify_signature(body, sig)

    def test_invalid_signature_raises(self):
        from fastapi import HTTPException

        from apps.api.routes.webhooks import _verify_signature

        body = b'{"action": "opened"}'
        with patch("apps.api.routes.webhooks.settings") as mock_settings:
            mock_settings.github_webhook_secret = "real-secret"
            with pytest.raises(HTTPException) as exc_info:
                _verify_signature(body, "sha256=invalidsig")
            assert exc_info.value.status_code == 401

    def test_missing_signature_raises(self):
        from fastapi import HTTPException

        from apps.api.routes.webhooks import _verify_signature

        body = b'{"action": "opened"}'
        with patch("apps.api.routes.webhooks.settings") as mock_settings:
            mock_settings.github_webhook_secret = "real-secret"
            with pytest.raises(HTTPException) as exc_info:
                _verify_signature(body, None)
            assert exc_info.value.status_code == 401

    def test_no_secret_configured_skips_verification(self):
        from apps.api.routes.webhooks import _verify_signature

        body = b'{"action": "opened"}'
        with patch("apps.api.routes.webhooks.settings") as mock_settings:
            mock_settings.github_webhook_secret = "changeme"
            # Should not raise even with wrong/missing signature
            _verify_signature(body, "sha256=wrong")


class TestWebhookPayloadParsing:
    """Tests for webhook payload structure."""

    def test_bug_payload_structure(self, bug_payload):
        assert bug_payload["action"] == "opened"
        assert bug_payload["issue"]["number"] == 42
        assert bug_payload["issue"]["title"] == "App crashes when uploading file larger than 10MB"
        assert bug_payload["repository"]["full_name"] == "acme/myapp"
        assert bug_payload["installation"]["id"] == 99001

    def test_question_payload_structure(self, question_payload):
        assert question_payload["action"] == "opened"
        assert question_payload["issue"]["number"] == 43

    def test_minimal_payload_empty_body(self, minimal_payload):
        assert minimal_payload["issue"]["body"] == ""
        assert minimal_payload["issue"]["title"] == "Something is broken"

    def test_all_fixtures_have_installation(self, bug_payload, question_payload, minimal_payload):
        for payload in [bug_payload, question_payload, minimal_payload]:
            assert "installation" in payload
            assert "id" in payload["installation"]


class TestWebhookEndpointIntegration:
    """Tests for the webhook endpoint using test client."""

    def _make_signature(self, body: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_health_check(self, api_client):
        resp = api_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_webhook_ignores_non_issues_event(self, api_client, bug_payload):
        body = json.dumps(bug_payload).encode()
        headers = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-push-001",
            "X-Hub-Signature-256": "sha256=skip",  # No secret configured in test
        }
        resp = api_client.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_webhook_ignores_issues_non_opened(self, api_client, bug_payload):
        payload = {**bug_payload, "action": "closed"}
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-closed-001",
            "X-Hub-Signature-256": "sha256=skip",
        }
        resp = api_client.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_webhook_accepts_issues_opened(self, api_client, bug_payload):
        body = json.dumps(bug_payload).encode()
        headers = {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-bug-001",
            "X-Hub-Signature-256": "sha256=skip",
        }
        resp = api_client.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "enqueued"

    def test_webhook_idempotent_on_duplicate_delivery(self, api_client, bug_payload):
        body = json.dumps(bug_payload).encode()
        headers = {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-idempotent-001",
            "X-Hub-Signature-256": "sha256=skip",
        }
        # First request
        resp1 = api_client.post("/webhooks/github", content=body, headers=headers)
        assert resp1.status_code == 200

        # Second request with same delivery_id
        resp2 = api_client.post("/webhooks/github", content=body, headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate"
