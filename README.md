# ProofLoop — Stage 2 setup (Windows)

Stage 2 adds the first live research workflow to the Stage 1 React/Vite and FastAPI foundation. The backend creates a structured plan with Ollama, searches with Tavily, reads public pages through Firecrawl, extracts fields with verbatim supporting excerpts, saves task/source/finding records in Supabase, and streams progress over SSE. Research values are source-supported, not independently cross-checked.

Stage 3 recovery, retries, resumability and development fault injection; Stage 4 artifact generation; and Stage 5 MCP integration are not implemented.

## Prerequisites

- Windows 10/11, Python 3.11+, Node.js 20+ with npm
- [Ollama](https://ollama.com/download) and an installed model (default `qwen3:4b`)
- Supabase project with the migration applied
- Tavily API key and Firecrawl API key

## Configure Supabase

In the Supabase SQL editor, run [`backend/supabase/migrations/20261015_stage2_research.sql`](backend/supabase/migrations/20261015_stage2_research.sql). It creates `research_tasks`, `research_steps`, `sources` and `findings`, with indexes, foreign keys, unique constraints and owner-scoped RLS policies.

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

`backend/.env.example` documents all Stage 2 configuration. Search queries, pages per task, page-content length, total task duration, provider timeouts and concurrency are configurable. Concurrency defaults to one to limit load on the local model and external providers. This stage deliberately does not retry failed calls. A failed page is recorded as a limitation and the workflow continues with remaining candidates; search/planning or persistence failures fail the task.

SSE progress is held in memory while the backend process runs. Completed task/source/finding records are persisted, but progress event history is not durable across backend restarts. Persistent resumability is Stage 3 work.

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
