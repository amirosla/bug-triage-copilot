#!/usr/bin/env python3
"""Send a test webhook payload to the local API.

Usage:
    python scripts/send_test_webhook.py [--fixture bug|question|minimal]

Requires the API to be running at http://localhost:8000
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures"
API_URL = os.getenv("API_URL", "http://localhost:8000")
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "changeme")


def load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / f"issue_opened_{name}.json"
    if not path.exists():
        print(f"ERROR: Fixture not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        payload = json.load(f)
    # Assign a unique delivery ID + issue ID to avoid idempotency conflicts
    delivery_id = f"test-delivery-{uuid.uuid4().hex[:8]}"
    payload["issue"]["id"] = int(time.time() * 1000) % 2**31  # unique ID
    return payload, delivery_id


def sign_payload(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send test webhook to Bug Triage Copilot API")
    parser.add_argument(
        "--fixture",
        choices=["bug", "question", "minimal"],
        default="bug",
        help="Fixture file to use (default: bug)",
    )
    args = parser.parse_args()

    payload, delivery_id = load_fixture(args.fixture)
    body = json.dumps(payload).encode()
    signature = sign_payload(body, WEBHOOK_SECRET)

    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": signature,
        "User-Agent": "GitHub-Hookshot/test",
    }

    print(f"Sending {args.fixture} webhook to {API_URL}/webhooks/github")
    print(f"  Delivery ID : {delivery_id}")
    print(f"  Issue       : #{payload['issue']['number']} — {payload['issue']['title']}")
    print(f"  Repo        : {payload['repository']['full_name']}")
    print()

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(f"{API_URL}/webhooks/github", content=body, headers=headers)
    except httpx.ConnectError:
        print(f"ERROR: Could not connect to {API_URL}. Is the API running?", file=sys.stderr)
        sys.exit(1)

    print(f"Response: HTTP {resp.status_code}")
    try:
        print(f"Body    : {json.dumps(resp.json(), indent=2)}")
    except Exception:
        print(f"Body    : {resp.text}")

    if resp.status_code == 200:
        data = resp.json()
        if data.get("status") == "enqueued":
            print()
            print("✓ Webhook accepted! Check the worker logs for triage output.")
            print(f"  Dashboard: {API_URL}/")
        elif data.get("status") == "duplicate":
            print()
            print("ℹ Duplicate delivery — no new job created.")
    else:
        print(f"\nWARNING: Unexpected status code {resp.status_code}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
