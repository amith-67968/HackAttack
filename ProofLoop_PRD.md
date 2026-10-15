# ProofLoop — Product Requirements Document (PRD)

**Document status:** Implementation-ready draft  
**Product:** ProofLoop  
**Product type:** Autonomous deep-research and deliverable-generation agent  
**Target implementation:** Codex-assisted development

---

## 1. Product Summary

ProofLoop is an AI research agent that accepts natural-language requests, investigates multiple online sources, extracts information, verifies important claims, recovers from tool failures, and produces a requested deliverable.

Supported deliverables include Excel spreadsheets, email drafts, and research reports.

The key differentiator is **evidence-backed research with resumable execution and honest completion reporting**. The agent must not claim success merely because an LLM generated a response or a tool returned a success message.

### Example requests

- Find 20 internships in Bangalore with stipends, eligibility, skills, deadlines and application links.
- Find 20 physiotherapy centres in Bangalore and compare their published consultation costs.
- Compare 20 engineering colleges in Bangalore using a clearly identified ranking methodology.
- Research internships and draft an email summarizing the strongest opportunities.
- Generate 200 AI project ideas grouped by domain and difficulty and export them to Excel.

### Product workflow

1. Understand the user's request and desired output.
2. Generate and persist an execution plan.
3. Discover candidate sources.
4. Read source pages and extract evidence-backed claims.
5. Cross-check important facts and investigate missing information.
6. Verify data and flag contradictions.
7. Persist progress and recover from failures.
8. Generate the requested deliverable.
9. Verify the generated deliverable.
10. Report results and unresolved limitations.
11. Request explicit approval before sending emails or performing other sensitive external actions.

---

## 2. Product Goals and Non-Goals

### Goals

- Accept flexible natural-language research requests.
- Conduct multi-source research instead of relying only on search snippets.
- Maintain source provenance for important findings.
- Distinguish verified, unverified, missing and conflicting information.
- Display progress while the research runs.
- Save execution state and resume interrupted tasks.
- Prevent duplicate work when retrying operations.
- Generate downloadable, source-backed deliverables.
- Run the LLM locally through Ollama by default.
- Use a controlled Python tool layer and demonstrate MCP integration.
- Keep the MVP manageable for a hackathon while allowing later expansion.

### Non-goals for the initial release

- Guaranteed discovery of every relevant webpage.
- Guaranteed correctness of every third-party source.
- Training a custom language model.
- Building a search engine for the entire internet.
- Unrestricted control of the user's computer.
- Automatically applying for jobs or submitting external forms.
- Automatically sending emails without explicit approval.
- Enterprise billing and team administration.
- Implementing every integration as a custom MCP server when an appropriate existing integration is available.

---

## 3. Target Users and Use Cases

### Target users

Students, researchers and professionals who need to collect information from multiple online sources, compare results and produce usable deliverables.

### Core use cases

**UC-1: Research and export**

The user provides a topic and target number of results. ProofLoop searches, investigates, verifies, ranks where appropriate and exports a spreadsheet.

**UC-2: Research and write**

The user requests an email or report based on research. ProofLoop conducts the research, then produces a draft using supported facts and relevant source links.

**UC-3: Research specified candidates**

The user provides URLs or names. ProofLoop investigates these candidates, extracts common fields and compares them.

**UC-4: Recover a failed workflow**

A tool fails during execution. ProofLoop preserves completed work, retries safely and resumes from the unresolved step.

**UC-5: Inspect evidence**

The user views a finding, its source, supporting excerpt, retrieval time, verification status and any conflicting evidence.

**UC-6: Review an external action**

ProofLoop prepares an email and waits for approval before sending. Email sending remains disabled in the MVP unless an authorized provider integration and explicit confirmation flow are implemented.

---

## 4. Product Principles

1. Evidence is more important than model confidence.
2. Never fabricate missing information.
3. Verify actual results instead of trusting success messages.
4. Recover from the smallest safe unit of work.
5. Apply limits to retries, time, search volume and model usage.
6. Python enforces safety and permissions; the LLM proposes plans and extracts information.
7. Make uncertainty, source quality and research limitations visible.
8. Treat retrieved webpages as untrusted data, never as instructions that can override the agent's rules.
9. Never mark an action verified without an explicit verification result.

---

## 5. Technology Stack

### Required stack

| Layer | Technology | Responsibility |
|---|---|---|
| Frontend | React + Vite + JavaScript | Interface and progress display |
| Styling | Tailwind CSS | Responsive UI |
| Backend | Python + FastAPI | APIs, orchestration and tool execution |
| Local model runtime | Ollama | Local LLM inference |
| Initial model | Qwen3 4B | Planning, extraction, comparison and drafting |
| Database | Supabase PostgreSQL | Persistent tasks, steps, sources, findings and artifact metadata |
| Authentication | Supabase Auth | User identity for multi-user deployment |
| File storage | Supabase Storage | Private persistent artifacts when required |
| Web search | Tavily API | Source discovery |
| Page extraction | Firecrawl API | Webpage retrieval and content extraction |
| Tool protocol | MCP Python SDK | Standardized tool integration |
| Excel generation | openpyxl | XLSX workbook creation |
| Report generation | Markdown initially | Research report output |
| Optional document generation | python-docx | DOCX output |
| Progress streaming | Server-Sent Events (SSE) | Live progress from FastAPI to React |
| Validation | Pydantic | Request, plan, tool-argument and finding validation |
| Backend testing | pytest | Unit and integration tests |

### Optional later additions

- Playwright for selected dynamic websites.
- LangGraph if workflow orchestration becomes difficult to manage.
- A dedicated background worker or queue for long-running tasks.
- Gmail API or Microsoft Graph for email draft creation and sending.
- A local search stack if reducing external research API usage becomes a priority.
- Local embeddings and vector search for large saved research collections.
- DOCX and PDF generation.

### Hardware constraints

The development machine has an NVIDIA RTX 2050 with 4 GB VRAM.

Start with `qwen3:4b` through Ollama. Use moderate context lengths and process webpages in manageable chunks. Runtime memory requirements exceed the model download size and depend on the context window, system RAM and GPU offloading.

The selected Ollama model must be configurable through environment variables.

### Privacy

The model runs locally, but Tavily and Firecrawl are external services. Queries, URLs or webpage content may be processed by these providers.

Do not claim that the entire system is fully private while external services are in use. API keys must remain on the backend.

---

## 6. System Architecture

### High-level workflow

1. React submits a research request to FastAPI.
2. FastAPI validates the request and creates a Supabase task.
3. The local LLM creates a structured plan.
4. Python validates the plan and persists the steps.
5. The execution controller calls registered tools through adapters or MCP.
6. Sources and retrieved metadata are stored.
7. Structured findings are extracted and linked to supporting evidence.
8. Verification checks required fields, duplicates, conflicts and source support.
9. Missing or weak evidence can trigger additional research rounds.
10. The output generator creates the requested artifact.
11. The backend validates the artifact or queries the destination to verify its contents.
12. Final status, summary and artifact metadata are saved.
13. React displays the results, limitations and download options.

### Suggested backend structure

```text
backend/
  app/
    main.py
    core/
      config.py
      logging.py
      security.py
    api/
      routes/
        health.py
        research.py
        artifacts.py
        approvals.py
    schemas/
      research.py
      plan.py
      evidence.py
      artifacts.py
    services/
      planner.py
      orchestrator.py
      research_engine.py
      extraction.py
      verification.py
      recovery.py
      ranking.py
      artifact_factory.py
      progress.py
    tools/
      registry.py
      search_tavily.py
      fetch_firecrawl.py
      mcp_client.py
    outputs/
      spreadsheet.py
      email.py
      report.py
    repositories/
      tasks.py
      steps.py
      sources.py
      findings.py
      artifacts.py
    tests/
  requirements.txt
  .env.example
```

Adapt this structure to an existing repository where appropriate. Avoid creating unnecessary abstractions.

### Frontend structure

```text
frontend/
  src/
    api/
      client.js
      research.js
    components/
      RequestComposer.jsx
      ResearchProgress.jsx
      StepTimeline.jsx
      SourceEvidencePanel.jsx
      FindingsTable.jsx
      VerificationSummary.jsx
      ArtifactPreview.jsx
      ApprovalDialog.jsx
    pages/
      ResearchPage.jsx
      TaskDetailPage.jsx
    hooks/
      useResearchEvents.js
    App.jsx
    main.jsx
```

Use a clean, modern interface with neutral colors. Prioritize research state, evidence and the actual deliverable over decorative animations.

---

## 7. User Interface Requirements

### Main screen

Provide:

- A natural-language research input.
- An output selector: Auto, Spreadsheet, Email Draft or Research Report.
- Optional target count, location, domain and requested fields.
- Research depth: Standard or Deep.
- A start-research button.
- Clear configuration errors.

The system should infer suitable spreadsheet columns rather than forcing the user to configure them manually.

### Running-task screen

Display real progress stages:

- Understanding request.
- Creating plan.
- Discovering sources.
- Reading webpages.
- Extracting findings.
- Verifying claims.
- Investigating evidence gaps.
- Generating output.
- Verifying output.
- Completed, completed with limitations, failed, cancelled or awaiting approval.

Do not invent percentages. Display a percentage only when it can be calculated from meaningful task progress.

### Evidence viewer

For each finding, display:

- Extracted fields.
- Source title and URL.
- Source domain.
- Retrieval or last-checked time.
- Supporting excerpt.
- Verification status.
- Conflicting sources.
- Missing information.
- Ranking explanation when relevant.

### Deliverable screen

For spreadsheets, preview the results and provide an XLSX download.

For emails, display the subject, body, recipient when provided, source links and editable draft.

For reports, display a Markdown-rendered report with citations and limitations.

### Approval screen

Before a sensitive external action, show the exact operation, recipient and content, then require explicit confirmation. Approval of one action does not authorize unrelated future actions.

---

## 8. Research Engine Requirements

### Input interpretation

Create a structured request containing:

- `query`: original user request.
- `topic`: normalized topic.
- `target_count`: requested number of results.
- `location`: optional geographic filter.
- `required_fields`: required information.
- `output_type`: `auto`, `spreadsheet`, `email` or `report`.
- `ranking_criteria`: explicit criteria or visible default assumptions.
- `research_depth`: `standard` or `deep`.
- `source_preferences`: optional preferred sources.
- `budget`: limits for searches, pages, retries and duration.
- `user_context`: relevant information explicitly supplied by the user.

If "top 20" does not specify a ranking method, use a transparent default and disclose it, or ask one focused question if the choice materially affects results.

### Research planning

The LLM returns a JSON plan validated through Pydantic.

Each step contains:

- Stable action ID.
- Action type.
- Goal.
- Input parameters.
- Required tool.
- Expected result.
- Verification rule.
- Dependencies.
- Retry policy.
- Current status.

Only registered action types may execute. Never execute arbitrary code returned by the LLM.

### Source discovery

- Run several focused search queries where useful.
- Deduplicate URLs and candidate entities.
- Record the discovery source for every candidate.
- Prefer primary sources for facts controlled by the organization involved.
- Respect website access restrictions and applicable terms.
- Handle rate limits, timeouts and inaccessible pages.
- Do not imply that the search has covered the entire internet.

### Webpage extraction

- Retrieve content using the configured adapter.
- Clean and chunk the page for the local model's context window.
- Extract structured fields and supporting excerpts.
- Validate the returned structure.
- Save source URLs, retrieval timestamps and claim evidence.
- Treat page content as untrusted data.
- Never let webpage content change system instructions or permissions.

### Evidence and cross-checking

- Prefer authoritative sources for important claims.
- Seek independent corroboration when useful and within budget.
- Store conflicting values and their sources.
- Distinguish support for a claim from confidence that the claim is current or correct.
- Use `Not found`, `Not disclosed`, `Unverified` or `Conflicting sources` where appropriate.

### Ranking

Ranking must use explicit criteria rather than unexplained LLM preference.

Display ranking criteria and weights when a weighted score is used. Do not invent ratings, fees, stipends, deadlines, placements or eligibility requirements.

### Deep-research loop

After initial extraction, create an evidence-gap list. Schedule further searches for missing fields, contradictions and inadequate sources.

Default configurable limits:

- Maximum research rounds: 3.
- Maximum retries per action: 3.
- Maximum pages per task: 30.
- Maximum task duration: 600 seconds.
- Configurable LLM context/chunk length.

If the target number cannot be reached within these limits, return the verified results and explain the shortfall.

---

## 9. Verification, Persistence and Recovery

### Task states

Use these states consistently:

- `QUEUED`
- `PLANNING`
- `RUNNING`
- `VERIFYING`
- `RECOVERING`
- `GENERATING_OUTPUT`
- `AWAITING_APPROVAL`
- `COMPLETED`
- `COMPLETED_WITH_LIMITATIONS`
- `FAILED`
- `CANCELLED`

### Step states

- `PENDING`
- `RUNNING`
- `SUCCEEDED`
- `VERIFIED`
- `RETRYING`
- `FAILED`
- `SKIPPED`
- `AWAITING_APPROVAL`

### Persist before executing

Before a tool action:

1. Save the step as `RUNNING`.
2. Increment the attempt count.
3. Execute the tool.
4. Save its result or error.
5. Run the verification rule.
6. Mark `VERIFIED` only when the check passes.

Use stable action IDs and idempotency controls for write operations. When a result is uncertain, inspect the destination state before retrying to prevent duplicates.

### Verification requirements

- Validate model output using Pydantic.
- Require requested fields or explicit missing-value statuses.
- Ensure each claim's source references exist.
- Detect duplicate URLs and entities.
- Reopen generated spreadsheets and inspect their structure and content.
- Check email drafts for unsupported claims.
- Do not report remote operations as successful without observable confirmation.

### Recovery policy

Retry transient failures such as timeouts, temporary server errors and connection failures. Respect rate-limit instructions.

Use bounded exponential backoff with jitter.

Do not retry permanent authorization or validation failures without corrective action.

Preserve successful steps, inspect uncertain side effects before repeating them, and stop when retry limits are reached.

Expose resumable tasks and cooperative cancellation.

### Failure simulation

Implement development-only fault injection that can deliberately fail a selected action once.

The demo should show the failure, retained progress, recovery attempt and final verification. Disable fault injection by default and make it unavailable in production mode.

---

## 10. Deliverable Generation

The research engine must be independent of the output format.

### Automatic output selection

- Spreadsheet or Excel request → Spreadsheet output.
- Email or message request → Email draft output.
- Report or summary request → Research report.
- Ambiguous output → Ask a concise clarification or use a documented default.

### Spreadsheet output

Use `openpyxl` to create `.xlsx` workbooks with filters, frozen headers, hyperlinks, wrapped text and sensible formatting.

Recommended worksheets:

1. `Results`: requested records and fields.
2. `Sources & Evidence`: source links, timestamps and evidence excerpts.
3. `Research Summary`: methodology, coverage, counts, limitations and unresolved questions.

Adapt columns to each request.

### Email draft output

- Generate a subject and body.
- Use source-supported information.
- Include useful source links when appropriate.
- Identify uncertainty honestly.
- Use a neutral professional tone if none is specified.
- Do not invent recipient addresses.
- Show an editable draft in the UI.
- Disable sending by default.

If sending is implemented later, use an authorized email integration and require explicit approval of the exact recipient, subject and body.

### Research report output

Support Markdown initially.

Include:

- Executive summary.
- Research methodology.
- Findings.
- Comparisons where relevant.
- Source citations.
- Limitations.
- Unresolved questions.

PDF and DOCX exports can be added later.

### Artifact storage

Save temporary local files in a controlled artifact directory with safe filenames.

For persistent artifacts, upload to a private Supabase Storage bucket. Return a short-lived signed URL after authorization. Do not make private artifacts public by default.

---

## 11. Supabase Data Model

Use UUID primary keys and timestamps. Create migrations or reproducible SQL setup scripts.

### `research_tasks`

- `id`: UUID primary key.
- `user_id`: UUID, nullable only in explicitly defined single-user development mode.
- `query`: TEXT, required.
- `normalized_topic`: TEXT.
- `output_type`: spreadsheet, email or report.
- `research_depth`: standard or deep.
- `status`: task-state enum.
- `config`: JSONB.
- `plan`: JSONB.
- `summary`: TEXT.
- `error_summary`: TEXT.
- `started_at`: TIMESTAMPTZ.
- `completed_at`: TIMESTAMPTZ.
- `created_at`, `updated_at`: TIMESTAMPTZ.

### `research_steps`

- `id`: UUID primary key.
- `task_id`: foreign key to `research_tasks`, cascading on delete.
- `action_key`: stable action identifier.
- `sequence_no`: integer.
- `action_type`: text.
- `title`: text.
- `input`: JSONB.
- `expected_result`: JSONB.
- `verification_rule`: JSONB.
- `status`: text.
- `attempt_count`: integer, default zero.
- `result`: JSONB.
- `last_error`: text.
- `started_at`, `completed_at`, `created_at`, `updated_at`: timestamps.
- Unique constraint on `(task_id, action_key)`.

### `sources`

- `id`: UUID primary key.
- `task_id`: task foreign key.
- `url`: original URL.
- `canonical_url`: normalized URL.
- `title`: source title.
- `domain`: source domain.
- `source_type`: source category.
- `retrieved_at`: timestamp.
- `published_at`: optional timestamp.
- `content_excerpt`: supporting content.
- `content_hash`: optional content hash.
- `quality_notes`: text.
- `fetch_status`: text.
- `created_at`: timestamp.
- Unique constraint on `(task_id, canonical_url)` when available.

### `findings`

- `id`: UUID primary key.
- `task_id`: task foreign key.
- `entity_key`: normalized entity identifier.
- `entity_name`: entity name.
- `field_name`: extracted field name.
- `field_value`: JSONB.
- `value_status`: found, not found, not disclosed, unverified or conflicting.
- `source_id`: source foreign key.
- `supporting_excerpt`: text.
- `verification_status`: text.
- `confidence_notes`: text.
- `created_at`, `updated_at`: timestamps.

### `verification_results`

- `id`: UUID primary key.
- `task_id`: task foreign key.
- `step_id`: optional step foreign key.
- `finding_id`: optional finding foreign key.
- `check_type`: text.
- `status`: text.
- `expected`: JSONB.
- `observed`: JSONB.
- `details`: text.
- `created_at`: timestamp.

### `artifacts`

- `id`: UUID primary key.
- `task_id`: task foreign key.
- `artifact_type`: text.
- `filename`: text.
- `storage_path`: optional Supabase Storage path.
- `local_path`: optional local development path.
- `mime_type`: text.
- `status`: text.
- `validation_summary`: JSONB.
- `created_at`: timestamp.

### Supabase security requirements

- Enable Row Level Security on user-owned tables.
- Restrict task and artifact access to the authenticated owner.
- Keep the service-role key on the backend only.
- Clearly separate single-user local development from multi-user deployment.
- Test RLS policies.
- Never expose private research artifacts publicly by default.

---

## 12. API Contract

All endpoints must validate inputs, use appropriate HTTP status codes and return consistent error shapes.

### `GET /api/health`

Returns API status and non-sensitive component availability, including Ollama connectivity. Never return secrets.

### `POST /api/research`

Create and start a task.

Example request:

```json
{
  "query": "Find 20 internships in Bangalore with stipend and eligibility requirements",
  "output_type": "spreadsheet",
  "research_depth": "deep",
  "target_count": 20,
  "options": {
    "location": "Bangalore",
    "required_fields": [
      "company",
      "role",
      "stipend",
      "eligibility",
      "skills",
      "deadline",
      "application_url"
    ],
    "max_pages": 30,
    "max_research_rounds": 3
  }
}
```

Example response:

```json
{
  "task_id": "<uuid>",
  "status": "QUEUED",
  "events_url": "/api/research/<uuid>/events"
}
```

### `GET /api/research/{task_id}`

Returns task details, plan summary, status, progress and errors or limitations.

### `GET /api/research/{task_id}/events`

Streams progress through Server-Sent Events.

Supported event types:

- `task_status`
- `step_started`
- `tool_call_started`
- `source_discovered`
- `finding_extracted`
- `verification_result`
- `retry_started`
- `step_completed`
- `artifact_ready`
- `approval_required`
- `task_completed`
- `task_error`

Do not stream secrets or excessively large webpage bodies.

### `GET /api/research/{task_id}/sources`

Returns authorized source metadata.

### `GET /api/research/{task_id}/findings`

Returns findings, evidence links and verification states.

### `POST /api/research/{task_id}/resume`

Resumes a recoverable task from unresolved steps after checking the actual state.

Reject completed, cancelled or unsafe-to-resume tasks.

### `POST /api/research/{task_id}/cancel`

Requests cooperative cancellation. Avoid starting additional steps after cancellation is requested.

### `GET /api/artifacts/{artifact_id}/download`

Authorizes access to the artifact, validates its status and serves the local file or a short-lived signed storage URL.

### `POST /api/research/{task_id}/approvals/{approval_id}`

Records approval or rejection of a specific external action. Approval must apply only to the action and content the user reviewed.

---

## 13. Tool Registry and MCP Integration

Every tool must be registered with:

- Name and description.
- Pydantic input schema.
- Implementation or adapter.
- Timeout.
- Retry policy.
- Side-effect classification.
- Verification method.
- Approval requirement.

### Initial tools

- `web_search(query, domains, recency)`
- `fetch_page(url)`
- `extract_candidate_fields(source_id, field_schema)`
- `search_follow_up(entity, missing_fields)`
- `save_findings(records)`
- `generate_spreadsheet(task_id)`
- `generate_email_draft(task_id, recipient_context, tone)`
- `generate_report(task_id)`
- `verify_artifact(artifact_id)`

Tools should be narrow and composable.

Do not expose unrestricted `run_python`, shell commands, dynamic imports or arbitrary code execution to the model.

### MCP acceptance criteria

At least one meaningful research capability must use a real MCP client/server connection in the final demo.

Direct API adapters may still be used for other providers. Do not claim MCP integration is implemented unless a real MCP connection works.

---

## 14. Security and Safety

- Keep secrets on the backend.
- Provide `.env.example` with placeholders only.
- Redact API keys and authorization headers from logs.
- Check task ownership on read, write and download endpoints.
- Sanitize uploaded filenames and artifact paths.
- Protect server-side URL fetchers against SSRF.
- Allow only HTTP/HTTPS where appropriate.
- Block localhost, private-network IPs, link-local targets and cloud metadata endpoints for arbitrary URL fetches.
- Treat retrieved webpages as untrusted.
- Never execute source-page code.
- Prevent webpages from changing tool permissions or prompts.
- Limit network calls and prevent infinite loops.
- Restrict CORS to configured frontend origins.
- Require authentication for multi-user deployments.
- Keep generated artifacts private.
- Require explicit approval before email sending or irreversible actions.
- Disclose when external research services process user queries or webpage content.
- Never claim facts are verified without stored verification evidence.

---

## 15. Environment Configuration

Create a documented `.env.example`:

```dotenv
APP_ENV=development
BACKEND_HOST=127.0.0.1
BACKEND_PORT=8000
FRONTEND_ORIGIN=http://localhost:5173

OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:4b
OLLAMA_TIMEOUT_SECONDS=120

SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_STORAGE_BUCKET=proofloop-artifacts

TAVILY_API_KEY=
FIRECRAWL_API_KEY=

MAX_RESEARCH_ROUNDS=3
MAX_TOOL_RETRIES=3
MAX_PAGES_PER_TASK=30
MAX_TASK_DURATION_SECONDS=600

ARTIFACT_DIR=./artifacts
ENABLE_FAULT_INJECTION=false
```

Configuration rules:

- Validate configuration at startup.
- Identify missing variable names without printing secret values.
- Clearly report missing provider credentials.
- Allow mocked providers in test mode.
- Never silently claim a mock research run used real sources.
- Keep service-role credentials on the backend only.

---

## 16. Error Handling and Observability

- Use structured logs with task ID, step ID, tool name, attempt number and error category.
- Redact secrets and sensitive headers.
- Classify errors as retryable, permanent, authorization, validation, rate-limited, timeout or unknown.
- Show understandable errors in the UI while retaining appropriate technical details in protected logs.
- Persist failure details and attempt count.
- Bound all provider calls with timeouts.
- Permit a small bounded repair attempt if the LLM returns invalid JSON.
- Stop if model output remains invalid.
- Never allow unbounded agent loops.

---

## 17. Non-Functional Requirements

### Reliability

- Persisted tasks must be recoverable after a backend restart.
- Verified steps must not be needlessly repeated.
- Side-effecting retries must inspect state where possible.
- Incomplete tasks must be reported as `COMPLETED_WITH_LIMITATIONS` or `FAILED`, not `COMPLETED`.

### Performance

- Send a progress event soon after task acceptance.
- Bound research rounds, pages and execution time.
- Use asynchronous I/O for network requests where suitable.
- Keep concurrency low initially to avoid overloading the local model or hitting service rate limits.
- Do not load entire large websites into the LLM context.

### Usability

- Show current stage and step status.
- Allow evidence inspection.
- Provide loading, empty, error and completion states.
- Support copying email drafts and downloading artifacts.

### Maintainability

- Isolate external providers behind adapters.
- Separate research from output generation.
- Test state, verification and recovery logic without external API keys.
- Use Pydantic schemas at API and model boundaries.

---

## 18. Staged Implementation Plan

The complete product is larger than a five-hour hackathon. Build a runnable vertical slice first.

### Stage 1 — Runnable foundation

Implement:

- React, Vite, JavaScript and Tailwind.
- FastAPI backend and `/api/health`.
- Ollama connectivity and configurable model.
- Supabase configuration and task repository skeleton.
- `.env.example`.
- Windows setup documentation.

**Exit criteria:** Frontend and backend start. The health endpoint works. Component availability is reported correctly. No secrets are hardcoded.

### Stage 2 — First research workflow

Implement:

- Natural-language request input.
- Task creation and persistence.
- Structured plan generation.
- Tavily search integration.
- Firecrawl page retrieval.
- Evidence-aware extraction.
- Supabase source and finding storage.
- SSE progress stream.

**Exit criteria:** A real research request produces findings with source URLs in the UI.

### Stage 3 — Verification and recovery

Implement:

- Structured extraction validation.
- Bounded retries.
- Persistent resumable states.
- URL and candidate deduplication.
- Follow-up searches for missing information.
- Development-only failure injection.

**Exit criteria:** A deliberately failed tool call recovers without losing completed work or creating duplicate results.

### Stage 4 — Deliverables

Implement:

- XLSX spreadsheet generation.
- Email draft generation.
- Markdown report generation.
- Artifact validation and download.

**Exit criteria:** The same research data can generate at least two output formats. Generated artifacts pass deterministic checks.

### Stage 5 — MCP and demonstration polish

Implement:

- At least one real MCP integration.
- Source evidence details.
- Completion and limitation summaries.
- Approval interface for future send actions.
- Development-only recovery demo controls.

**Exit criteria:** Demo research, source evidence, intentional failure, safe recovery and a verified output artifact.

### Feature priorities

**Must have:** Natural-language request, planning, search, page reading, local Ollama, persistent state, source provenance, basic verification, bounded retries, SSE progress, one verified output type and tests.

**Should have:** XLSX, email and Markdown outputs; evidence-gap loops; MCP integration; source-conflict display.

**Could have:** Advanced ranking, browser automation, DOCX/PDF export and email provider integration.

**Not in MVP:** Automatic job applications, automatic email sending, unrestricted computer control and arbitrary browser actions.

---

## 19. Acceptance Criteria

### Request and planning

- [ ] Submit a natural-language task through React.
- [ ] Persist the task and a structured action plan.
- [ ] Reject unknown action types and invalid tool arguments.
- [ ] Support spreadsheet, email draft and report output requests.

### Research and evidence

- [ ] Run multiple focused searches where appropriate.
- [ ] Read source pages for important claims instead of relying only on snippets.
- [ ] Link each finding to a source or mark it unsupported.
- [ ] Never fabricate missing values.
- [ ] Detect duplicate URLs and entities.
- [ ] Display unresolved conflicts and research gaps.

### Persistence and recovery

- [ ] Persist step state before and after execution.
- [ ] Resume a task after a backend restart.
- [ ] Retry transient failures within limits.
- [ ] Preserve completed work after a simulated failure.
- [ ] Avoid duplicate results during retries.
- [ ] Report exhausted retries honestly.

### Deliverables

- [ ] XLSX files contain correct headers and worksheets.
- [ ] Generated workbooks reopen and pass structural checks.
- [ ] Email output is a draft by default.
- [ ] Reports include citations and limitations.
- [ ] Artifact downloads enforce authorization.

### Safety

- [ ] No API keys in frontend bundles or source control.
- [ ] Webpage content cannot change tool permissions.
- [ ] No arbitrary code execution exposed to the LLM.
- [ ] Sensitive actions require explicit approval.
- [ ] Missing Ollama or configuration is reported accurately.
- [ ] External service usage is disclosed.

### Testing

- [ ] Unit tests cover validation, deduplication, verification and retries.
- [ ] Integration tests use mocks by default.
- [ ] At least one end-to-end research-to-artifact test passes.
- [ ] One fault-injection test verifies recovery.

---

## 20. Test Strategy

### Unit tests

Test:

- Plan and finding schemas.
- Valid state transitions.
- URL normalization and duplicate detection.
- Evidence-to-finding relationships.
- Missing and conflicting values.
- Retry classification and retry limits.
- Workbook generation and reopening.
- Email claim/source checks where implemented.

### Integration tests

Use mocked Ollama, Tavily and Firecrawl adapters where appropriate.

Cover:

1. Successful search, fetch, extraction and export.
2. Search returning no results.
3. Page timeout followed by success.
4. An operation succeeding despite a lost response.
5. Invalid LLM JSON and bounded recovery.
6. Supabase unavailable.
7. Ollama unavailable or model missing.
8. Artifact validation failure.
9. Cancellation during execution.
10. Attempted cross-user access.

### Manual demo

1. Submit a request for 10 internships in Bangalore with stipend, eligibility and application links.
2. Show source-backed findings.
3. Inject a temporary failure.
4. Show retained progress and recovery.
5. Show the validated spreadsheet and any unresolved fields.
6. Run a second request to draft an email based on those findings.

---

## 21. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Small local model creates weak plans | Strict schemas, bounded tasks and validation |
| Search finds stale or incomplete results | Diverse queries, primary sources and explicit coverage limits |
| API rate limits or cost | Budgets, caching and bounded retries |
| Websites block extraction | Flag inaccessible sources and try suitable alternatives |
| Agent repeats work after failure | Persistent steps, stable IDs and state inspection |
| Prompt injection in webpages | Treat content as untrusted and enforce a tool allowlist |
| Supabase data exposure | RLS, ownership checks and backend-only privileged credentials |
| Research appears frozen | SSE events and persisted progress |
| Ranking is arbitrary | Explicit criteria and visible methodology |
| Privacy claims are misleading | Disclose external research-service data flows |
| Scope exceeds hackathon time | Complete one vertical slice before expanding |

---

## 22. Definition of Done

The initial MVP is complete when a user can:

1. Enter a research request in React.
2. Have FastAPI create and execute a plan using the configured local Ollama model and research tools.
3. Store sources, findings and progress in Supabase.
4. Inspect supporting evidence and verification status.
5. Generate a downloadable deliverable.
6. Observe recovery from a forced transient failure.
7. Receive an honest final summary of results and limitations.

A polished interface alone does not satisfy the requirements. The demo must show evidence, persistence, verification, recovery and a real deliverable.

---

## 23. Instructions for Codex

Treat this PRD as the product contract.

1. Inspect the repository before modifying it.
2. Preserve existing useful code.
3. If the repository is empty, scaffold the frontend and backend using this stack.
4. Implement one stage at a time, starting with Stage 1.
5. Keep the application runnable after each stage.
6. Use small modules, Pydantic boundaries and provider adapters.
7. Do not fabricate live research data. Label mock data explicitly.
8. Build a working end-to-end workflow before expanding features.
9. Add tests alongside backend capabilities.
10. Never hardcode API keys or expose server secrets to React.
11. Document Windows setup, Ollama installation, model download, Supabase configuration and environment variables.
12. Add SQL migrations or reproducible schema setup for all database tables and RLS policies.
13. Report missing provider configuration clearly instead of pretending a live task succeeded.
14. At the end of each stage, report changed files, tests executed, results, known gaps and the next stage.
15. Ask before introducing major technology changes or paid dependencies outside this PRD.

### Initial prompt for Codex

Read `ProofLoop_PRD.md` completely. Inspect the repository before making changes and summarize the current structure.

Implement Stage 1 only: React + Vite + JavaScript + Tailwind frontend, FastAPI backend, health endpoint, Ollama connectivity and model configuration checks, Supabase configuration and repository skeleton, `.env.example`, and Windows setup documentation.

Do not implement future stages yet. Do not hardcode credentials. Add basic tests and verify that frontend and backend can start.

At the end, summarize changed files, setup commands, tests executed, test results and any manual configuration still required.

---

## 24. Default Decisions

- **Authentication:** Supabase Auth for multi-user deployment; clearly marked single-user mode for local development.
- **Default output:** Infer from the user's explicit request. Ask a clarification when the output is materially ambiguous.
- **Email sending:** Draft-only for MVP.
- **Research depth:** Deep, subject to configured limits.
- **MCP:** At least one real MCP integration in the demo.
- **File storage:** Local temporary artifacts initially; private Supabase Storage for persistent artifacts.
- **Local model:** `qwen3:4b`, configurable without changing application code.
- **Frontend language:** JavaScript.
- **Backend language:** Python.
- **Deployment:** Local development first. A remote backend must not assume it can connect directly to the developer's laptop Ollama instance.

---

**End of ProofLoop PRD**