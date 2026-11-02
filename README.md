# ToolSmithAI

ToolSmithAI lets business users ask questions about connected SQL databases or uploaded CSV/Excel datasets in plain language. It discovers and profiles metadata, retrieves relevant schema evidence with embeddings, generates read-only SQL through the AI-first fast path, validates the SQL, executes it against current data, and returns tables, KPIs, grounded insights, and charts.

## What is included

- SQL Server connection, discovery, profiling, dictionary, and search
- AI-first natural-language question answering for connected SQL sources
- Vector/embedding retrieval for relevant schema and business evidence
- Deterministic SQL safety validation before every execution
- Persistent validated-plan cache; cached plans are revalidated and SQL is re-executed
- Follow-up question context and clarification flows
- CSV and Excel upload/report workflows
- CSV, Excel, and PDF export
- Optional report email delivery and scheduled workflows
- React web interface and FastAPI backend

Runtime databases, credentials, uploaded files, exports, logs, dependency folders, backups, and generated validation results are intentionally excluded from Git.

## Current architecture

```text
User question
  -> React AI Workspace
  -> POST /v1/composer/ask
  -> source/dataset selection
       SQL source
         -> vector retrieval of schema + business evidence
         -> one AI-first intent/schema/SQL generation call
         -> relationship completion and deterministic validation
         -> live read-only SQL execution
         -> deterministic table/KPI/insight/chart formatting
       CSV or Excel dataset
         -> dataset report and analysis pipeline
  -> answer, evidence, export, and follow-up state
```

The retired keyword intent router and legacy multi-agent SQL execution path are not part of the active composer route.

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer
- Microsoft ODBC Driver for SQL Server when connecting to SQL Server
- An OpenAI API key for AI-first SQL question answering

## Local setup

### 1. Backend

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with your own secrets and feature configuration. Never commit `.env`.

For backend development and testing, install the additional test dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

Start the API:

```powershell
python -m uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
```

The application creates its local SQLite metadata database on first startup. For a durable location outside the repository, set `TOOLSMITH_DATA_DIR` in `.env` before starting.

### 2. Frontend

In a second terminal:

```powershell
Set-Location frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/v1` requests to the backend at `http://127.0.0.1:8000`.

## Configuration

Use `.env.example` as the supported configuration template. At minimum, configure secure values for authentication/encryption and provide `OPENAI_API_KEY` when enabling AI functionality.

The AI-first SQL fast path is controlled by feature flags and a source allow-list. Keep it disabled for a source until that source has completed metadata preparation and validation. Real email delivery is also disabled by default.

## Tests

Backend:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests
```

Frontend:

```powershell
Set-Location frontend
npm test
```

Tests use isolated fixtures unless a test is explicitly marked as a live integration check. Do not point automated tests at a production database.

## Repository layout

```text
api/                 FastAPI routes and response adapters
auth/                Authentication helpers
core/sqlservice/     AI-first retrieval, generation, validation, cache, and execution
core/answering/      Answer, insight, chart, and export formatting
data/                Metadata persistence and datasource services
frontend/            React/Vite web application
migrations/          Additive SQLite metadata migrations
tests/               Backend regression tests
docs/                Architecture and validation documentation
knowledge_packages/  Sanitized portable knowledge packages
```

## Data and security rules

- Generated SQL is read-only and must pass deterministic validation.
- A plan-cache hit never returns old result rows; SQL runs again against current source data.
- Raw query rows, credentials, secrets, prompts, and vectors are not stored in the durable plan cache.
- Local `.env`, SQLite files, logs, uploads, exports, and backups are not version controlled.
- Permanently deleting a datasource is distinct from disconnecting it and requires explicit confirmation in the UI.

## Docker

```powershell
docker build -t toolsmithai .
docker run --rm -p 8000:8000 --env-file .env -v ${PWD}/data:/app/data toolsmithai
```

For production, use a dedicated persistent data directory and a proper secret-management system rather than storing credentials in the image or repository.
