# 🤖 Bug Triage Copilot

> A production-grade GitHub App that automatically triages new Issues using LLM analysis, semantic similarity search, and a real-time dashboard.

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16+pgvector-4169E1?logo=postgresql)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker)
![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?logo=githubactions)

---

## What it does

When a GitHub Issue is opened in any connected repository, Bug Triage Copilot:

1. **Receives** the GitHub webhook and stores the event
2. **Enqueues** an async triage job (RQ + Redis)
3. **Redacts** secrets from the issue body before sending to LLM
4. **Analyses** the issue with an LLM to produce:
   - 📋 3–6 bullet summary
   - 🎯 Priority (P0–P3) with justification
   - 🏷️ Suggested labels with confidence scores
   - ❓ Clarifying questions (when info is missing)
   - 🔁 Reproduction steps (when inferable)
5. **Embeds** the issue using a vector model and finds **similar past issues** via cosine similarity (pgvector)
6. **Posts a Markdown comment** on the GitHub Issue with the full analysis
7. **Optionally applies labels** if confidence exceeds the configured threshold
8. **Displays results** in a server-side rendered **dashboard**

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        GitHub                                    │
│  Issues.opened  ──webhook──►  POST /webhooks/github             │
└─────────────────────────────────────────────────────────────────┘
                                      │
                              ┌───────▼───────┐
                              │   FastAPI API  │  :8000
                              │               │  • webhook receiver
                              │               │  • REST /api/*
                              │               │  • UI (Jinja2)
                              └───────┬───────┘
                                      │ enqueue
                              ┌───────▼───────┐
                              │  Redis Queue  │  (RQ)
                              └───────┬───────┘
                                      │ dequeue
                              ┌───────▼───────┐
                              │  RQ Worker    │
                              │               │
                              │  1. redact    │
                              │  2. LLM call  │──► OpenAI / Mock
                              │  3. embed     │
                              │  4. pgvector  │──► Similar issues
                              │  5. comment   │──► GitHub API
                              └───────┬───────┘
                                      │
                              ┌───────▼───────┐
                              │  PostgreSQL   │
                              │  + pgvector   │
                              │               │
                              │  repos        │
                              │  issues       │
                              │  triage_res.  │
                              │  embeddings   │
                              │  deliveries   │
                              └───────────────┘
```

---

## Quick Start

### Prerequisites

- Docker & Docker Compose
- (Optional) A GitHub App for real webhook integration

### 1. Clone & configure

```bash
git clone <your-repo-url> bug-triage-copilot
cd bug-triage-copilot

# Copy and edit environment variables
cp .env.example .env
# Edit .env — at minimum set GITHUB_WEBHOOK_SECRET
```

### 2. Start all services

```bash
docker compose up --build
```

This starts:
- **PostgreSQL** with pgvector extension (`:5432`)
- **Redis** (`:6379`)
- **API** — runs Alembic migrations then starts FastAPI (`:8000`)
- **Worker** — RQ worker consuming the `triage` queue

### 3. Open the dashboard

```
http://localhost:8000/
```

### 4. Test with a mock webhook

```bash
# Install dependencies locally for the script
pip install httpx

# Send a sample bug report
python scripts/send_test_webhook.py --fixture bug

# Send a question issue
python scripts/send_test_webhook.py --fixture question

# Send a minimal (empty body) issue
python scripts/send_test_webhook.py --fixture minimal
```

Or with raw curl:

```bash
# Generate signature (replace 'changeme' with your GITHUB_WEBHOOK_SECRET)
PAYLOAD=$(cat tests/fixtures/issue_opened_bug.json)
SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "changeme" | awk '{print "sha256="$2}')

curl -X POST http://localhost:8000/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -H "X-GitHub-Delivery: test-$(date +%s)" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$PAYLOAD"
```

---

## GitHub App Setup

1. Go to **GitHub Settings → Developer settings → GitHub Apps → New GitHub App**

2. Configure the app:
   - **Webhook URL**: `https://your-domain/webhooks/github`
   - **Webhook Secret**: a random secret string
   - **Permissions**:
     - Issues: Read & Write
     - Metadata: Read

3. Install the app on your repository

4. Set environment variables:
   ```env
   GITHUB_APP_ID=123456
   GITHUB_PRIVATE_KEY_BASE64=$(base64 -w0 your-private-key.pem)
   GITHUB_WEBHOOK_SECRET=your-secret
   ```

5. For local development, use **[smee.io](https://smee.io)** or **[ngrok](https://ngrok.com)** to proxy webhooks:
   ```bash
   # Using smee-client
   npm install -g smee-client
   smee --url https://smee.io/YOUR_CHANNEL --target http://localhost:8000/webhooks/github
   ```

---

## LLM Configuration

The app supports a **mock provider** (default) and any **OpenAI-compatible** API:

```env
# Use mock (no API key, great for development)
LLM_PROVIDER=mock

# Use OpenAI
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini

# Use Groq (OpenAI-compatible)
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=gsk_...
LLM_MODEL=llama-3.3-70b-versatile

# Use Ollama (local)
LLM_PROVIDER=openai
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=llama3.2
```

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhooks/github` | GitHub webhook receiver |
| `GET` | `/api/repos` | List registered repos |
| `PATCH` | `/api/repos/{id}/config` | Update repo triage config |
| `GET` | `/api/issues` | List issues (filterable) |
| `GET` | `/api/issues/{id}` | Issue detail with triage |
| `GET` | `/api/deliveries` | Recent webhook deliveries |
| `GET` | `/` | Dashboard UI |
| `GET` | `/issues/{id}` | Issue detail UI |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Swagger UI |

### Update repo configuration

```bash
curl -X PATCH http://localhost:8000/api/repos/{repo_id}/config \
  -H 'Content-Type: application/json' \
  -d '{
    "allowed_labels": ["bug", "enhancement", "documentation", "question"],
    "auto_apply_labels": false,
    "label_confidence_threshold": 0.75,
    "similarity_threshold": 0.80
  }'
```

---

## Running Tests

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run unit tests (no external services needed)
PYTHONPATH=packages:. pytest tests/unit/ -v

# Run all tests including integration (SQLite in-memory)
PYTHONPATH=packages:. pytest tests/ -v

# With coverage report
PYTHONPATH=packages:. pytest tests/ --cov --cov-report=html
```

---

## Project Structure

```
bug-triage-copilot/
├── apps/
│   ├── api/                    # FastAPI application
│   │   ├── main.py             # App factory + middleware
│   │   ├── routes/
│   │   │   ├── webhooks.py     # POST /webhooks/github
│   │   │   ├── api.py          # REST API endpoints
│   │   │   └── ui.py           # Jinja2 UI routes
│   │   └── templates/          # HTML templates (Tailwind CSS)
│   └── worker/
│       ├── main.py             # RQ worker entry point
│       └── jobs/
│           └── triage.py       # Full triage pipeline
├── packages/
│   └── core/                   # Shared domain logic
│       ├── config.py           # Settings (pydantic-settings)
│       ├── db/session.py       # SQLAlchemy session factory
│       ├── models/
│       │   ├── db.py           # ORM models (5 tables)
│       │   └── schemas.py      # Pydantic schemas (I/O + LLM)
│       └── services/
│           ├── llm_client.py   # LLM abstraction + Mock + OpenAI
│           ├── github_client.py # GitHub App auth + API calls
│           ├── embedding_service.py  # pgvector similarity search
│           └── secret_redaction.py   # Regex-based secret masking
├── migrations/                 # Alembic migrations
│   ├── alembic.ini
│   ├── env.py
│   └── versions/001_initial.py
├── tests/
│   ├── fixtures/               # Sample GitHub webhook payloads
│   ├── unit/                   # Unit tests (no external deps)
│   └── integration/            # Integration tests (SQLite)
├── scripts/
│   └── send_test_webhook.py    # Dev helper to test locally
├── docker-compose.yml
├── Dockerfile.api
├── Dockerfile.worker
├── Makefile
└── .env.example
```

---

## Design Decisions

### Idempotency via Delivery ID

Every GitHub webhook contains a unique `X-GitHub-Delivery` header. On receipt, the API immediately checks the `webhook_deliveries` table for this ID. If it already exists, the request returns `200 duplicate` without processing. This prevents double-triaging from GitHub retries or network hiccups.

### Queue + Worker architecture

The webhook handler only writes to DB and enqueues a job — it responds in < 50ms. All expensive work (LLM calls, GitHub API, embedding computation) happens in the RQ worker. This means:
- No GitHub webhook timeouts
- Workers can be scaled independently
- Failed jobs are tracked with error details

### JSON validation of LLM output

The LLM is instructed to return structured JSON. The output is validated against a strict Pydantic schema (`TriageOutput`). If validation fails, the worker automatically attempts a **repair prompt** — asking the LLM to fix its own malformed output. If both attempts fail, the issue is marked `failed` for manual review.

### Secret redaction before LLM

Before any issue body is sent to an external LLM, `secret_redaction.py` applies a suite of regex patterns covering GitHub tokens, AWS keys, private keys, JWTs, and generic long base64/hex strings. Redacted text is what the LLM sees; the original body is stored in the database.

### pgvector similarity search

Issue embeddings are stored using PostgreSQL's `pgvector` extension with an HNSW index for approximate nearest-neighbour search. If pgvector is unavailable, the service falls back to Python-level cosine similarity computation. The similarity threshold is configurable per repository.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+psycopg://triage:triage@localhost:5432/bug_triage` | PostgreSQL URL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis URL |
| `GITHUB_APP_ID` | — | GitHub App ID |
| `GITHUB_PRIVATE_KEY_BASE64` | — | Base64-encoded PEM private key |
| `GITHUB_WEBHOOK_SECRET` | `changeme` | Webhook HMAC secret |
| `LLM_PROVIDER` | `mock` | `mock` or `openai` |
| `LLM_API_KEY` | `sk-mock` | API key for LLM provider |
| `LLM_MODEL` | `gpt-4o-mini` | Model name |
| `LLM_BASE_URL` | _(OpenAI default)_ | Custom endpoint for OpenAI-compatible APIs |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `EMBEDDING_DIMENSIONS` | `1536` | Embedding vector dimensions |
| `LOG_LEVEL` | `INFO` | Structured JSON log level |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API | FastAPI 0.115 + Uvicorn |
| Queue | RQ 2.x + Redis 7 |
| Database | PostgreSQL 16 + pgvector |
| ORM | SQLAlchemy 2.x + Alembic |
| AI | OpenAI-compatible API (pluggable) |
| Validation | Pydantic v2 |
| Logging | structlog (JSON) |
| UI | Jinja2 + Tailwind CSS |
| Auth | PyJWT (RS256) + GitHub App |
| HTTP | httpx + tenacity (retry/backoff) |
| Containers | Docker Compose |
| CI | GitHub Actions |

---

---

# 🤖 Bug Triage Copilot *(wersja polska)*

> GitHub App klasy produkcyjnej, która automatycznie triażuje nowe zgłoszenia (Issues) za pomocą analizy LLM, semantycznego wyszukiwania podobnych przypadków i dashboardu w czasie rzeczywistym.

---

## Co robi

Gdy w połączonym repozytorium zostanie otwarte nowe Issue, Bug Triage Copilot:

1. **Odbiera** webhook od GitHub i zapisuje zdarzenie w bazie
2. **Kolejkuje** asynchroniczne zadanie triażu (RQ + Redis)
3. **Redaguje** tajne dane z treści zgłoszenia przed wysłaniem do LLM
4. **Analizuje** zgłoszenie za pomocą LLM, generując:
   - 📋 Streszczenie w 3–6 punktach
   - 🎯 Priorytet (P0–P3) z uzasadnieniem
   - 🏷️ Sugerowane etykiety z poziomem pewności
   - ❓ Pytania wyjaśniające (gdy brakuje informacji)
   - 🔁 Kroki reprodukcji (gdy można je wywnioskować)
5. **Wektoryzuje** zgłoszenie i wyszukuje **podobne wcześniejsze przypadki** przez cosinus podobieństwa (pgvector)
6. **Publikuje komentarz Markdown** na GitHub Issue z pełną analizą
7. **Opcjonalnie przypisuje etykiety**, gdy poziom pewności przekracza skonfigurowany próg
8. **Wyświetla wyniki** w renderowanym po stronie serwera **dashboardzie**

---

## Architektura

```
┌─────────────────────────────────────────────────────────────────┐
│                        GitHub                                    │
│  Issues.opened  ──webhook──►  POST /webhooks/github             │
└─────────────────────────────────────────────────────────────────┘
                                      │
                              ┌───────▼───────┐
                              │   FastAPI API  │  :8000
                              │               │  • odbiornik webhooków
                              │               │  • REST /api/*
                              │               │  • UI (Jinja2)
                              └───────┬───────┘
                                      │ kolejkowanie
                              ┌───────▼───────┐
                              │  Redis Queue  │  (RQ)
                              └───────┬───────┘
                                      │ przetwarzanie
                              ┌───────▼───────┐
                              │  RQ Worker    │
                              │               │
                              │  1. redakcja  │
                              │  2. LLM       │──► OpenAI / Mock
                              │  3. embedding │
                              │  4. pgvector  │──► Podobne zgłoszenia
                              │  5. komentarz │──► GitHub API
                              └───────┬───────┘
                                      │
                              ┌───────▼───────┐
                              │  PostgreSQL   │
                              │  + pgvector   │
                              │               │
                              │  repos        │
                              │  issues       │
                              │  triage_res.  │
                              │  embeddings   │
                              │  deliveries   │
                              └───────────────┘
```

---

## Szybki start

### Wymagania

- Docker i Docker Compose
- (Opcjonalnie) GitHub App do prawdziwej integracji webhooków

### 1. Sklonuj i skonfiguruj

```bash
git clone <adres-repozytorium> bug-triage-copilot
cd bug-triage-copilot

# Skopiuj i uzupełnij zmienne środowiskowe
cp .env.example .env
# Edytuj .env — minimum: ustaw GITHUB_WEBHOOK_SECRET
```

### 2. Uruchom wszystkie usługi

```bash
docker compose up --build
```

Uruchamia:
- **PostgreSQL** z rozszerzeniem pgvector (`:5432`)
- **Redis** (`:6379`)
- **API** — uruchamia migracje Alembic, następnie FastAPI (`:8000`)
- **Worker** — worker RQ obsługujący kolejkę `triage`

### 3. Otwórz dashboard

```
http://localhost:8000/
```

### 4. Przetestuj z przykładowym webhookiem

```bash
# Zainstaluj zależności lokalnie
pip install httpx

# Wyślij przykładowy raport błędu
python scripts/send_test_webhook.py --fixture bug

# Wyślij zgłoszenie z pytaniem
python scripts/send_test_webhook.py --fixture question

# Wyślij minimalne zgłoszenie (bez treści)
python scripts/send_test_webhook.py --fixture minimal
```

Lub za pomocą curl:

```bash
# Wygeneruj podpis (zamień 'changeme' na swój GITHUB_WEBHOOK_SECRET)
PAYLOAD=$(cat tests/fixtures/issue_opened_bug.json)
SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "changeme" | awk '{print "sha256="$2}')

curl -X POST http://localhost:8000/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -H "X-GitHub-Delivery: test-$(date +%s)" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$PAYLOAD"
```

---

## Konfiguracja GitHub App

1. Przejdź do **GitHub Settings → Developer settings → GitHub Apps → New GitHub App**

2. Skonfiguruj aplikację:
   - **Webhook URL**: `https://twoja-domena/webhooks/github`
   - **Webhook Secret**: losowy tajny ciąg znaków
   - **Uprawnienia**:
     - Issues: Read & Write
     - Metadata: Read

3. Zainstaluj aplikację w swoim repozytorium

4. Ustaw zmienne środowiskowe:
   ```env
   GITHUB_APP_ID=123456
   GITHUB_PRIVATE_KEY_BASE64=$(base64 -w0 twoj-klucz-prywatny.pem)
   GITHUB_WEBHOOK_SECRET=twoj-sekret
   ```

5. Przy lokalnym developmencie użyj **[smee.io](https://smee.io)** lub **[ngrok](https://ngrok.com)** do proxowania webhooków:
   ```bash
   npm install -g smee-client
   smee --url https://smee.io/TWOJ_KANAL --target http://localhost:8000/webhooks/github
   ```

---

## Konfiguracja LLM

Aplikacja obsługuje **wbudowanego mocka** (domyślnie) oraz dowolne **API kompatybilne z OpenAI**:

```env
# Mock — bez klucza API, idealne do developmentu
LLM_PROVIDER=mock

# OpenAI
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini

# Groq (kompatybilny z OpenAI)
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=gsk_...
LLM_MODEL=llama-3.3-70b-versatile

# Ollama (lokalnie)
LLM_PROVIDER=openai
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=llama3.2
```

---

## API

| Metoda | Ścieżka | Opis |
|--------|---------|------|
| `POST` | `/webhooks/github` | Odbiornik webhooków GitHub |
| `GET` | `/api/repos` | Lista zarejestrowanych repozytoriów |
| `PATCH` | `/api/repos/{id}/config` | Aktualizacja konfiguracji triażu |
| `GET` | `/api/issues` | Lista zgłoszeń (z filtrowaniem) |
| `GET` | `/api/issues/{id}` | Szczegóły zgłoszenia z triażem |
| `GET` | `/api/deliveries` | Ostatnie dostarczenia webhooków |
| `GET` | `/` | Dashboard UI |
| `GET` | `/issues/{id}` | Widok szczegółów zgłoszenia |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Swagger UI |

### Aktualizacja konfiguracji repozytorium

```bash
curl -X PATCH http://localhost:8000/api/repos/{repo_id}/config \
  -H 'Content-Type: application/json' \
  -d '{
    "allowed_labels": ["bug", "enhancement", "documentation", "question"],
    "auto_apply_labels": false,
    "label_confidence_threshold": 0.75,
    "similarity_threshold": 0.80
  }'
```

---

## Uruchamianie testów

```bash
# Zainstaluj zależności deweloperskie
pip install -r requirements-dev.txt

# Testy jednostkowe (bez zewnętrznych usług)
PYTHONPATH=packages:. pytest tests/unit/ -v

# Wszystkie testy wraz z integracyjnymi (SQLite in-memory)
PYTHONPATH=packages:. pytest tests/ -v

# Z raportem pokrycia kodu
PYTHONPATH=packages:. pytest tests/ --cov --cov-report=html
```

---

## Struktura projektu

```
bug-triage-copilot/
├── apps/
│   ├── api/                    # Aplikacja FastAPI
│   │   ├── main.py             # Fabryka aplikacji + middleware
│   │   ├── routes/
│   │   │   ├── webhooks.py     # POST /webhooks/github
│   │   │   ├── api.py          # Endpointy REST API
│   │   │   └── ui.py           # Trasy Jinja2 UI
│   │   └── templates/          # Szablony HTML (Tailwind CSS)
│   └── worker/
│       ├── main.py             # Punkt wejścia workera RQ
│       └── jobs/
│           └── triage.py       # Pełny pipeline triażu
├── packages/
│   └── core/                   # Współdzielona logika domenowa
│       ├── config.py           # Ustawienia (pydantic-settings)
│       ├── db/session.py       # Fabryka sesji SQLAlchemy
│       ├── models/
│       │   ├── db.py           # Modele ORM (5 tabel)
│       │   └── schemas.py      # Schematy Pydantic (I/O + LLM)
│       └── services/
│           ├── llm_client.py        # Abstrakcja LLM + Mock + OpenAI
│           ├── github_client.py     # Auth GitHub App + wywołania API
│           ├── embedding_service.py # Wyszukiwanie podobieństwa pgvector
│           └── secret_redaction.py  # Maskowanie sekretów regexem
├── migrations/                 # Migracje Alembic
│   ├── alembic.ini
│   ├── env.py
│   └── versions/001_initial.py
├── tests/
│   ├── fixtures/               # Przykładowe payloady webhooków GitHub
│   ├── unit/                   # Testy jednostkowe (bez zewnętrznych zależności)
│   └── integration/            # Testy integracyjne (SQLite)
├── scripts/
│   └── send_test_webhook.py    # Narzędzie pomocnicze do testów lokalnych
├── docker-compose.yml
├── Dockerfile.api
├── Dockerfile.worker
├── Makefile
└── .env.example
```

---

## Decyzje projektowe

### Idempotentność przez Delivery ID

Każdy webhook od GitHub zawiera unikalny nagłówek `X-GitHub-Delivery`. Po otrzymaniu API natychmiast sprawdza tabelę `webhook_deliveries` pod kątem tego ID. Jeśli już istnieje, zapytanie zwraca `200 duplicate` bez przetwarzania. Zapobiega to podwójnemu triażowi spowodowanemu ponownymi próbami GitHub lub problemami sieciowymi.

### Architektura kolejka + worker

Obsługa webhooka jedynie zapisuje do bazy i kolejkuje zadanie — odpowiada w < 50 ms. Całą ciężką pracę (wywołania LLM, GitHub API, obliczanie embedingów) wykonuje worker RQ. Oznacza to:
- Brak timeoutów webhooków GitHub
- Niezależne skalowanie workerów
- Rejestrowanie błędów zadań ze szczegółami

### Walidacja JSON z wyjścia LLM

LLM jest instruowany, by zwracać strukturalny JSON. Wynik jest walidowany przez surowy schemat Pydantic (`TriageOutput`). Jeśli walidacja się nie powiedzie, worker automatycznie próbuje **repair prompt** — prosi LLM o poprawienie własnego błędnego wyniku. Jeśli obie próby zawiodą, zgłoszenie jest oznaczane jako `failed` do ręcznego przeglądu.

### Redakcja sekretów przed LLM

Zanim treść zgłoszenia zostanie wysłana do zewnętrznego LLM, `secret_redaction.py` stosuje zestaw wyrażeń regularnych obejmujących tokeny GitHub, klucze AWS, klucze prywatne, JWT-y i generyczne długie ciągi base64/hex. To zredagowana wersja trafia do LLM; oryginalna treść jest przechowywana w bazie.

### Wyszukiwanie podobieństwa przez pgvector

Embeddingi zgłoszeń są przechowywane za pomocą rozszerzenia PostgreSQL `pgvector` z indeksem HNSW do przybliżonego wyszukiwania najbliższych sąsiadów. Jeśli pgvector jest niedostępny, serwis przełącza się na obliczanie podobieństwa cosinusowego w Pythonie. Próg podobieństwa jest konfigurowalny dla każdego repozytorium.

---

## Zmienne środowiskowe

| Zmienna | Domyślnie | Opis |
|---------|-----------|------|
| `DATABASE_URL` | `postgresql+psycopg://triage:triage@localhost:5432/bug_triage` | URL PostgreSQL |
| `REDIS_URL` | `redis://localhost:6379/0` | URL Redis |
| `GITHUB_APP_ID` | — | ID GitHub App |
| `GITHUB_PRIVATE_KEY_BASE64` | — | Klucz prywatny PEM zakodowany w Base64 |
| `GITHUB_WEBHOOK_SECRET` | `changeme` | Sekret HMAC webhooków |
| `LLM_PROVIDER` | `mock` | `mock` lub `openai` |
| `LLM_API_KEY` | `sk-mock` | Klucz API dostawcy LLM |
| `LLM_MODEL` | `gpt-4o-mini` | Nazwa modelu |
| `LLM_BASE_URL` | _(domyślny OpenAI)_ | Własny endpoint dla API kompatybilnych z OpenAI |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Model embeddingów |
| `EMBEDDING_DIMENSIONS` | `1536` | Wymiarowość wektora embeddingu |
| `LOG_LEVEL` | `INFO` | Poziom logowania (strukturalny JSON) |

---

## Stack technologiczny

| Warstwa | Technologia |
|---------|-------------|
| API | FastAPI 0.115 + Uvicorn |
| Kolejka | RQ 2.x + Redis 7 |
| Baza danych | PostgreSQL 16 + pgvector |
| ORM | SQLAlchemy 2.x + Alembic |
| AI | API kompatybilne z OpenAI (wymienne) |
| Walidacja | Pydantic v2 |
| Logowanie | structlog (JSON) |
| UI | Jinja2 + Tailwind CSS |
| Auth | PyJWT (RS256) + GitHub App |
| HTTP | httpx + tenacity (retry/backoff) |
| Kontenery | Docker Compose |
| CI | GitHub Actions |
