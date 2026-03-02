.PHONY: help up down build logs migrate shell-api shell-worker test lint typecheck clean dev-install

# Default target
help:
	@echo "Bug Triage Copilot — Development Commands"
	@echo ""
	@echo "  make up          Start all services (Docker Compose)"
	@echo "  make down        Stop all services"
	@echo "  make build       Rebuild Docker images"
	@echo "  make logs        Tail all service logs"
	@echo "  make migrate     Run Alembic migrations"
	@echo "  make test        Run test suite"
	@echo "  make lint        Run ruff linter"
	@echo "  make typecheck   Run mypy type checker"
	@echo "  make dev-install Install dev dependencies locally"
	@echo "  make clean       Remove containers and volumes"
	@echo "  make webhook-test Send a test webhook payload"

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build --no-cache

logs:
	docker compose logs -f

logs-api:
	docker compose logs -f api

logs-worker:
	docker compose logs -f worker

migrate:
	PYTHONPATH=packages:. alembic -c migrations/alembic.ini upgrade head

shell-api:
	docker compose exec api bash

shell-worker:
	docker compose exec worker bash

shell-db:
	docker compose exec db psql -U triage -d bug_triage

dev-install:
	pip install -r requirements-dev.txt

test:
	PYTHONPATH=packages:. pytest tests/ -v --tb=short

test-cov:
	PYTHONPATH=packages:. pytest tests/ -v --cov --cov-report=term-missing

lint:
	ruff check packages/ apps/ tests/

lint-fix:
	ruff check --fix packages/ apps/ tests/

typecheck:
	PYTHONPATH=packages:. mypy packages/ apps/

clean:
	docker compose down -v --remove-orphans
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true

# Test a webhook locally (requires running API + valid GITHUB_WEBHOOK_SECRET)
webhook-test:
	@echo "Sending test webhook payload..."
	@python scripts/send_test_webhook.py

# Generate a new GitHub App private key (dev helper)
gen-key:
	openssl genrsa -out /tmp/dev-key.pem 2048 && \
	base64 -w0 /tmp/dev-key.pem > /tmp/dev-key.b64 && \
	echo "Base64 key written to /tmp/dev-key.b64" && \
	cat /tmp/dev-key.b64
