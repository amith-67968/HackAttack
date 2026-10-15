# ProofLoop â€” Stage 3 setup (Windows)

Stage 2 adds the first live research workflow to the Stage 1 React/Vite and FastAPI foundation. The backend creates a structured plan with Ollama, searches with Tavily, reads public pages through Firecrawl, extracts fields with verbatim supporting excerpts, saves task/source/finding records in Supabase, and streams progress over SSE. Research values are source-supported, not independently cross-checked.

Stage 3 adds bounded provider retries, durable attempt and event history, safe task resumption, cancellation and development-only failure injection. Stage 4 artifact generation and Stage 5 MCP integrations are not implemented.

## Prerequisites

- Windows 10/11, Python 3.11+, Node.js 20+ with npm
- [Ollama](https://ollama.com/download) and an installed model (default `qwen3:4b`)
- Supabase project with the migration applied
- Tavily API key and Firecrawl API key

## Configure Supabase

In the Supabase SQL editor, run [`backend/supabase/migrations/20261015_stage2_research.sql`](backend/supabase/migrations/20261015_stage2_research.sql), then [`backend/supabase/migrations/20261016_stage3_recovery.sql`](backend/supabase/migrations/20261016_stage3_recovery.sql), then [`backend/supabase/migrations/20261017_stage3_resume_claim.sql`](backend/supabase/migrations/20261017_stage3_resume_claim.sql). Stage 3 adds task leases/cancellation, attempt history, durable SSE events, supporting page content and database transition guards. Rollback scripts are provided alongside those migrations; reverse order is required. Apply migrations to the same project configured by `SUPABASE_URL`; after SQL Editor changes, allow PostgREST's schema cache to refresh before restarting the API.

Stage 2 runs in documented local single-user mode: the backend uses its service-role key, and the API must remain bound to loopback (`127.0.0.1`). Authentication and ownership checks for a remotely exposed API are not implemented in this stage. Never put the service-role key in frontend configuration.

## Configure and start the backend

From PowerShell at the repository root:

```powershell
cd backend
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Edit `backend\.env` and provide:

- `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from Supabase (the anon key remains optional in this service-role-only local workflow).
- `TAVILY_API_KEY` and `FIRECRAWL_API_KEY` from those provider accounts.
- `OLLAMA_MODEL` set to an installed model; install the default with `ollama pull qwen3:4b`.

Start Ollama, then run the backend from the `backend` directory:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Check `http://127.0.0.1:8000/api/health`. Provider configuration status appears there; research cannot start if a required provider key is missing or the configured Ollama model is unreachable/uninstalled. Error responses list the missing variable names without exposing values.

## Start the frontend

In another PowerShell window:

```powershell
cd frontend
npm install
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
npm run dev
```

Open the Vite URL (normally `http://localhost:5173`). The frontend only reads `VITE_API_BASE_URL`; do not add provider keys or Supabase service-role credentials to a `VITE_` variable. Queries are sent to Tavily and candidate URLs/page content are processed by Firecrawl.

## Limits

`backend/.env.example` documents all variables. Search query count, pages per task, page-content length, total task duration, provider timeouts, research concurrency, retry attempts and backoff are configurable. Concurrency defaults to one. `MAX_PROVIDER_ATTEMPTS` defaults to three; authentication, authorization and validation errors are not retried. Keep `FAILURE_INJECTION_ENABLED=false` except for local test runs. When explicitly enabled in development/test, the UI offers deterministic simulated failures; production requests reject failure injection.

Task attempts and SSE event history are persisted. Reconnecting clients replay from their last event sequence; task execution resumes through `POST /api/research/{task_id}/resume`, and cancellation is requested through `POST /api/research/{task_id}/cancel`. External GET calls may be repeated after an ambiguous outcome; source/finding writes use stable keys and reconciliation, so exactly-once provider execution is not claimed. The API uses service-role credentials in local single-user mode and is restricted to loopback. Do not expose it publicly without adding authenticated ownership enforcement.

## Tests and build

Backend, from `backend` with the virtual environment active:

```powershell
python -m pytest
```

Frontend, from `frontend`:

```powershell
npm test
npm run build
```

Provider calls are mocked in the automated tests; no API keys or live research calls are needed to run the test suite.
