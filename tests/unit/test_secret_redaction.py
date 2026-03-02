"""Unit tests for secret redaction service."""

from __future__ import annotations

import pytest

from core.services.secret_redaction import REDACTION_PLACEHOLDER, redact, redact_secrets


class TestGitHubTokenRedaction:
    def test_redacts_github_pat(self):
        text = "Use ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12 as your token"
        result = redact(text)
        assert "ghp_" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_redacts_github_oauth_token(self):
        text = "token: gho_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12"
        result = redact(text)
        assert "gho_" not in result

    def test_redacts_github_server_token(self):
        text = "ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde123456"
        result = redact(text)
        assert "ghs_" not in result


class TestAWSKeyRedaction:
    def test_redacts_aws_access_key(self):
        text = "My AWS key is AKIAIOSFODNN7EXAMPLE and I use it everywhere"
        result = redact(text)
        assert "AKIA" not in result
        assert REDACTION_PLACEHOLDER in result


class TestPrivateKeyRedaction:
    def test_redacts_rsa_private_key(self):
        text = (
            "Here is my key:\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xHn/ygWep4PAtEsHAMu3e\n"
            "-----END RSA PRIVATE KEY-----\n"
            "Please keep it safe."
        )
        result = redact(text)
        assert "BEGIN RSA PRIVATE KEY" not in result
        assert REDACTION_PLACEHOLDER in result


class TestConnectionStringRedaction:
    def test_redacts_postgres_url(self):
        text = "Connect to postgresql://admin:supersecret@db.example.com:5432/prod"
        result = redact(text)
        assert "supersecret" not in result

    def test_redacts_redis_url(self):
        text = "REDIS_URL=redis://:mypassword@redis.example.com:6379/0"
        result = redact(text)
        assert "mypassword" not in result


class TestJWTRedaction:
    def test_redacts_jwt_token(self):
        jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        text = f"Authorization: Bearer {jwt}"
        result = redact(text)
        assert "eyJhbGci" not in result


class TestSafeContent:
    def test_plain_text_unchanged(self):
        text = "This is a normal bug report without any secrets."
        result = redact(text)
        assert result == text

    def test_code_snippet_mostly_preserved(self):
        text = "def hello():\n    return 'world'\n\nprint(hello())"
        result = redact(text)
        # Short strings should not be redacted
        assert "def hello" in result

    def test_returns_found_secrets_list(self):
        text = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcde12 secret"
        _, found = redact_secrets(text)
        assert len(found) > 0
        assert any("github_token" in f for f in found)

    def test_empty_string(self):
        assert redact("") == ""

    def test_none_ish_empty(self):
        assert redact("   ") == "   "
