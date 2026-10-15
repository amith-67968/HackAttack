import React, { useEffect, useState } from 'react'
import { Activity, ArrowUpRight, Check, CircleHelp, Cpu, Database, Github, RefreshCw, ShieldCheck, Waves } from 'lucide-react'
import { artifactDownloadUrl, cancelResearch, createResearch, createResearchArtifact, getHealth, getResearchArtifacts, resumeResearch } from './api/client'
import { useResearchEvents } from './hooks/useResearchEvents'

function StatusDot({ state }) {
  const good = ['available', 'configured'].includes(state)
  return <span className={`status-dot ${good ? 'good' : 'muted'}`} aria-label={state} />
}

function StatusCard({ icon: Icon, title, status, detail, badge }) {
  return <article className="status-card">
    <div className="card-top"><div className="icon-box"><Icon size={18} /></div><StatusDot state={status} /></div>
    <div className="card-label">{title}</div>
    <div className="card-state">{badge}</div>
    <p>{detail}</p>
  </article>
}

const eventLabel = (event) => {
  const { type, data } = event
  if (type === 'task_status') return data.message || `Task status: ${data.status}`
  if (type === 'step_started') return `Started: ${data.title || data.action_type}`
  if (type === 'tool_call_started') return `Calling ${data.tool}${data.query ? `: ${data.query}` : ''}`
  if (type === 'mcp_tool_invocation') return `MCP protocol invoked ${data.tool_name} (${data.status})`
  if (type === 'source_discovered') return `Source found: ${data.title || data.url}`
  if (type === 'finding_extracted') return `Extracted ${data.count} field(s) from ${data.entity_name || 'source'}`
  if (type === 'step_completed') return `${data.title || 'Step'} â€” ${data.status}${data.message ? `: ${data.message}` : ''}`
  if (type === 'task_error') return `Task failed: ${data.message}`
  if (type === 'retry_started') return `Retrying ${data.tool || data.action_type} (attempt ${data.attempt}/${data.max_attempts})${data.error_classification ? ` after ${data.error_classification}` : ''}`
  if (type === 'step_failed') return `${data.title || data.action_type} failed${data.message ? `: ${data.message}` : ''}`
  if (type === 'task_resumed' || type === 'recovery_started') return 'Recovery worker started from saved progress.'
  if (type === 'task_cancel_requested') return 'Cancellation requested; the current safe operation will finish before stopping.'
  if (type === 'worker_interrupted') return 'Worker stopped unexpectedly; saved progress is available for recovery.'
  if (type === 'tool_call_finished') return `${data.tool || 'Tool'} finished (${data.status || 'recorded'})`
  if (type === 'verification_result') return `Verification: ${data.status || data.result || 'recorded'}`
  return data.summary || `Research finished with status ${data.status}`
}

function ResearchWorkspace({ components }) {
  const [query, setQuery] = useState('')
  const [fields, setFields] = useState('')
  const [location, setLocation] = useState('')
  const [domain, setDomain] = useState('')
  const [targetCount, setTargetCount] = useState('')
  const [depth, setDepth] = useState('standard')
  const [outputType, setOutputType] = useState('auto')
  const [failureInjection, setFailureInjection] = useState('')
  const [artifactType, setArtifactType] = useState('spreadsheet')
  const [recipient, setRecipient] = useState('')
  const [artifacts, setArtifacts] = useState([])
  const [artifactError, setArtifactError] = useState('')
  const [generatingArtifact, setGeneratingArtifact] = useState(false)
  const [emailDraft, setEmailDraft] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [refreshError, setRefreshError] = useState('')
  const [taskId, setTaskId] = useState(() => window.localStorage.getItem('proofloop-current-task') || '')
  const { events, task, sources, findings, connectionError, liveStatus, reconnecting, refreshSaved, reconnect } = useResearchEvents(taskId)
  const failureInjectionAvailable = components?.recovery?.failure_injection_enabled === true

  useEffect(() => {
    if (!taskId) { setArtifacts([]); return }
    getResearchArtifacts(taskId).then((result) => setArtifacts(result.artifacts || [])).catch(() => {})
  }, [taskId])

  async function generateArtifact() {
    setGeneratingArtifact(true); setArtifactError('')
    try {
      const artifact = await createResearchArtifact(taskId, { artifact_type: artifactType, recipient: recipient || null })
      setArtifacts((current) => [artifact, ...current.filter((item) => item.id !== artifact.id)])
      if (artifact.artifact_type === 'email') setEmailDraft(artifact.preview?.body || '')
    } catch (error) { setArtifactError(error.message) }
    finally { setGeneratingArtifact(false) }
  }

  function downloadEditedEmail() {
    const blob = new Blob([emailDraft], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url; link.download = 'proofloop-email-draft.txt'; link.click()
    URL.revokeObjectURL(url)
  }

  async function submit(event) {
    event.preventDefault()
    setSubmitting(true)
    setSubmitError('')
    setTaskId('')
    window.localStorage.removeItem('proofloop-current-task')
    try {
      const result = await createResearch({
        query,
        output_type: outputType,
        research_depth: depth,
        target_count: targetCount ? Number(targetCount) : null,
        options: {
          location: location || null,
          domain: domain || null,
          required_fields: fields.split(',').map((item) => item.trim()).filter(Boolean),
        },
        failure_injection: failureInjection || null,
      })
      window.localStorage.setItem('proofloop-current-task', result.task_id)
      setTaskId(result.task_id)
    } catch (error) { setSubmitError(error.message) }
    finally { setSubmitting(false) }
  }

  async function resume() {
    setSubmitError('')
    try { await resumeResearch(taskId); reconnect(); await refreshSaved() }
    catch (error) { setSubmitError(error.message) }
  }

  async function cancel() {
    setSubmitError('')
    try { await cancelResearch(taskId); await refreshSaved() }
    catch (error) { setSubmitError(error.message) }
  }

  return <section className="research-workspace" id="research-workspace">
    <div className="section-heading research-heading"><div><div className="eyebrow">FIRST RESEARCH WORKFLOW</div><h2>Start a research task</h2><p>Your request is planned with Ollama, searched with Tavily and read with Firecrawl.</p></div></div>
    <div className="research-provider-status" aria-label="Research provider configuration">
      {[['Ollama', components?.ollama?.model_available ? 'Model available' : 'Not ready'], ['Supabase', components?.supabase?.status === 'configured' ? 'Configured' : 'Not configured'], ['Tavily', components?.tavily?.status === 'configured' ? 'Configured' : 'Not configured'], ['Firecrawl', components?.firecrawl?.status === 'configured' ? 'Configured' : 'Not configured']].map(([name, state]) => <span key={name}><i className={state === 'Configured' || state === 'Model available' ? 'ready' : ''} />{name}: {state}</span>)}
    </div>
    <form className="research-form" onSubmit={submit}>
      <label className="field-label" htmlFor="research-query">What would you like to research?</label>
      <textarea id="research-query" value={query} onChange={(event) => setQuery(event.target.value)} minLength={5} maxLength={4000} required placeholder="For example: Find physiotherapy centres in Bangalore and compare their published consultation costs." />
      <div className="form-grid">
        <label>Fields to extract <span>comma separated</span><input value={fields} onChange={(event) => setFields(event.target.value)} placeholder="name, price, eligibility" /></label>
        <label>Location<input value={location} onChange={(event) => setLocation(event.target.value)} placeholder="Bangalore" /></label>
        <label>Preferred domain<input value={domain} onChange={(event) => setDomain(event.target.value)} placeholder="example.org" /></label>
        <label>Target count<input type="number" min="1" max="100" value={targetCount} onChange={(event) => setTargetCount(event.target.value)} placeholder="Optional" /></label>
        <label>Research depth<select value={depth} onChange={(event) => setDepth(event.target.value)}><option value="standard">Standard</option><option value="deep">Deep</option></select></label>
        <label>Output preference<select value={outputType} onChange={(event) => setOutputType(event.target.value)}><option value="auto">Auto</option><option value="spreadsheet">Spreadsheet</option><option value="email">Email draft</option><option value="report">Research report</option></select></label>
      </div>
      {failureInjectionAvailable && <label className="field-label">Development failure simulation <span>test only</span><select value={failureInjection} onChange={(event) => setFailureInjection(event.target.value)}><option value="">Disabled</option><option value="search_once">Search fails once, then succeeds</option><option value="page_timeout">Page retrieval times out</option><option value="after_source_persist">Source save response is lost</option><option value="lost_finding_response">Finding save response is lost</option><option value="restart_after_step">Simulate worker restart after a step</option><option value="exhaust_retries">Exhaust transient retries</option></select></label>}
      {submitError && <div className="error-banner" role="alert"><strong>Could not start research</strong><p>{submitError}</p></div>}
      <button type="submit" className="start-button" disabled={submitting}>{submitting ? 'Startingâ€¦' : 'Start research'} <ArrowUpRight size={15} /></button>
      <p className="provider-disclosure">Queries go to Tavily and source URLs/content to Firecrawl. Source-backed values are not independently cross-checked by a second source in this workflow.</p>
    </form>

    {taskId && <div className="research-result" aria-live="polite">
      <div className="result-title"><div><span className="eyebrow">TASK {taskId.slice(0, 8)}</span><h3>{task?.status || liveStatus}</h3></div><span className="no-progress">Live events Â· no estimated percentage</span></div>
      {connectionError && <div className="error-banner" role="status"><strong>Progress connection {reconnecting ? 'Â· reconnecting' : ''}</strong><p>{connectionError}</p><button className="check-task-button" onClick={() => { setRefreshError(''); refreshSaved().catch((error) => setRefreshError(error.message)) }}>Check saved task status</button>{refreshError && <p>{refreshError}</p>}</div>}
      {task?.summary && <p className="task-summary">{task.summary}</p>}
      {task?.error_summary && <p className="task-limitation">{task.error_summary}</p>}
      {task && (task.resume_eligible || task.cancel_eligible) && <div className="recovery-actions"><strong>{task.status === 'COMPLETED_WITH_LIMITATIONS' ? 'Some steps remain recoverable.' : task.status === 'FAILED' ? 'This task may be resumable.' : 'Task controls'}</strong>{task.resume_eligible && <button className="check-task-button" onClick={resume}><RefreshCw size={14} /> Resume from saved progress</button>}{task.cancel_eligible && <button className="check-task-button" onClick={cancel}>Request cancellation</button>}{submitError && <p role="alert">{submitError}</p>}</div>}
      <ol className="event-list">{events.map((event) => <li key={`${event.id}-${event.type}`} className={`event-item ${event.type === 'task_error' ? 'event-error' : ''}`}><span className="event-marker" /><div><strong>{event.type.replaceAll('_', ' ')}</strong><p>{eventLabel(event)}</p></div></li>)}</ol>
      {sources.length > 0 && <section className="evidence-section"><h4>Sources <span>{sources.length}</span></h4>{sources.map((source) => <article className="source-row" key={source.id}><div><a href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a><small>{source.domain} Â· {source.fetch_status}{source.retrieved_at ? ` Â· ${new Date(source.retrieved_at).toLocaleString()}` : ''}</small></div><span className="source-state">{source.fetch_status}</span></article>)}</section>}
      {findings.length > 0 && <section className="evidence-section"><h4>Extracted findings <span>{findings.length}</span></h4>{findings.map((finding) => { const source = sources.find((item) => item.id === finding.source_id); return <article className="finding-row" key={finding.id}><div className="finding-top"><strong>{finding.entity_name}</strong><span className="evidence-status">{finding.verification_status.replaceAll('_', ' ')}</span></div><div className="finding-value"><span>{finding.field_name}</span><strong>{finding.field_value ?? finding.value_status.replaceAll('_', ' ')}</strong></div>{finding.supporting_excerpt && <blockquote>â€œ{finding.supporting_excerpt}â€</blockquote>}{source && <a className="finding-source-link" href={source.url} target="_blank" rel="noreferrer">Source: {source.title || source.url}</a>}<small>{finding.confidence_notes}</small></article>})}</section>}
      <section className="evidence-section artifact-section"><h4>Deliverables <span>drafts only</span></h4><div className="form-grid"><label>Format<select aria-label="Artifact format" value={artifactType} onChange={(event) => setArtifactType(event.target.value)}><option value="spreadsheet">Spreadsheet (.xlsx)</option><option value="report">Research report (.md)</option><option value="email">Email draft (.txt)</option></select></label>{artifactType === 'email' && <label>Recipient (optional)<input aria-label="Email recipient" type="email" value={recipient} onChange={(event) => setRecipient(event.target.value)} placeholder="name@example.com" /></label>}</div><button className="check-task-button" onClick={generateArtifact} disabled={generatingArtifact || !taskId}>{generatingArtifact ? 'Generating…' : 'Generate deliverable'}</button>{artifactError && <p role="alert">{artifactError}</p>}{artifacts.map((artifact) => <article className="finding-row" key={artifact.id}><div className="finding-top"><strong>{artifact.artifact_type} · {artifact.status}</strong><a href={artifactDownloadUrl(artifact.id)} download={artifact.filename}>Download {artifact.filename}</a></div>{artifact.artifact_type === 'spreadsheet' && <><p>{artifact.preview?.summary}</p><div className="artifact-table-wrap"><table><thead><tr><th>Entity</th><th>Field</th><th>Value</th><th>Evidence</th></tr></thead><tbody>{(artifact.preview?.rows || []).map((row, index) => <tr key={`${row.entity}-${row.field}-${index}`}><td>{row.entity}</td><td>{row.field}</td><td>{row.value}</td><td>{row.evidence?.map((item, i) => <a key={i} href={item.url} target="_blank" rel="noreferrer">{item.title || item.url}</a>)}</td></tr>)}</tbody></table></div></>}{artifact.artifact_type === 'report' && <pre className="artifact-preview">{artifact.preview?.markdown}</pre>}{artifact.artifact_type === 'email' && <><label>Email draft editor<textarea aria-label="Email draft editor" value={emailDraft || artifact.preview?.body || ''} onChange={(event) => setEmailDraft(event.target.value)} rows={12} /></label><button className="check-task-button" onClick={() => navigator.clipboard?.writeText(emailDraft || artifact.preview?.body || '')}>Copy draft</button><button className="check-task-button" onClick={downloadEditedEmail}>Download edited draft</button><small>This is a draft. ProofLoop does not send email.</small></>}</article>)}</section>
    </div>}
  </section>
}

export default function App() {
  const [health, setHealth] = useState(null)
  const [error, setError] = useState('')
  const [checking, setChecking] = useState(false)

  async function refresh() {
    setChecking(true)
    setError('')
    try { setHealth(await getHealth()) } catch (err) { setError(err.message); setHealth(null) }
    finally { setChecking(false) }
  }

  useEffect(() => { refresh() }, [])
  const components = health?.components
  const model = components?.ollama

  return <div className="app-shell min-h-screen bg-slate-50">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Waves size={20} strokeWidth={2.4} /></div><span>ProofLoop</span><span className="brand-beta">BETA</span></div>
      <div className="workspace-label">WORKSPACE</div>
      <button className="nav-item active" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}><Activity size={17} /> Overview</button>
      <button className="nav-item" onClick={() => document.getElementById('research-workspace')?.scrollIntoView({ behavior: 'smooth' })}><span className="nav-placeholder">âŒ˜</span> Research runs</button>
      <button className="nav-item" onClick={() => document.getElementById('research-workspace')?.scrollIntoView({ behavior: 'smooth' })}><Database size={17} /> Sources & evidence</button>
      <div className="sidebar-bottom"><div className="user-avatar">P</div><div><strong>Local workspace</strong><small>Single user Â· Development</small></div><CircleHelp size={17} className="help" /></div>
    </aside>

    <main className="main-content">
      <header className="topbar"><div className="breadcrumb">Workspace <span>/</span> <b>Overview</b></div><div className="top-actions"><span className="local-pill"><span /> Local environment</span><a className="github-link" href="https://github.com" aria-label="GitHub"><Github size={18} /></a></div></header>
      <div className="page-wrap">
        <section className="welcome-row"><div><div className="eyebrow">YOUR RESEARCH WORKSPACE</div><h1>Good research starts<br className="mobile-break" /> with a clear picture.</h1><p className="lede">Check your local services before you begin. ProofLoop reports what it can actually reach.</p></div><button className="refresh-button" onClick={refresh} disabled={checking}><RefreshCw size={15} className={checking ? 'spin' : ''} /> {checking ? 'Checkingâ€¦' : 'Refresh status'}</button></section>

        {error && <div role="alert" className="error-banner"><span>Backend unavailable</span><p>{error}. Start the FastAPI server and refresh this page.</p></div>}

        <ResearchWorkspace components={components} />

        <section className="section-block"><div className="section-heading"><div><h2>System status</h2><p>Live checks from your local backend</p></div><span className={`updated-pill ${health ? 'is-live' : ''}`}><span />{health ? 'LIVE CHECK' : 'WAITING FOR BACKEND'}</span></div>
          <div className="status-grid">
            <StatusCard icon={Activity} title="ProofLoop API" status={components?.api?.status || 'unknown'} badge={components?.api?.status === 'available' ? 'Operational' : 'Waiting'} detail={components?.api?.status === 'available' ? 'Backend is responding to health checks.' : 'Waiting for the local backend to respond.'} />
            <StatusCard icon={Cpu} title="Ollama runtime" status={model?.status || 'unknown'} badge={model?.connected ? 'Connected' : 'Not connected'} detail={model?.connected ? `Ollama is reachable at the configured local endpoint.` : 'Start Ollama to enable local model checks.'} />
            <StatusCard icon={Database} title="Supabase" status={components?.supabase?.status || 'unknown'} badge={components?.supabase?.status === 'configured' ? 'Configured' : 'Not configured'} detail={components?.supabase?.status === 'configured' ? 'Credentials found. Database connectivity has not been checked.' : 'Add backend credentials when you are ready to configure persistence.'} />
          </div>
        </section>

        <section className="section-block model-section"><div className="section-heading"><div><h2>Local model</h2><p>Selected in backend environment configuration</p></div><span className="config-label"><ShieldCheck size={14} /> BACKEND CONFIG</span></div>
          <div className="model-panel"><div className="model-avatar"><Cpu size={21} /></div><div className="model-info"><div className="model-name">{model?.model || 'qwen3:4b'} <span className={`model-state ${model?.model_available ? 'model-ok' : ''}`}><span />{model?.model_available ? 'Ready' : model?.connected ? 'Model missing' : 'Unverified'}</span></div><p>{model?.connected ? `${model.available_models?.length || 0} model${model.available_models?.length === 1 ? '' : 's'} available in Ollama` : 'Model availability will appear when Ollama responds.'}</p></div><div className="model-note">Change <code>OLLAMA_MODEL</code> in <code>backend/.env</code><br />to select another installed model.</div></div>
        </section>

        <section className="next-step"><div className="next-icon"><Check size={17} /></div><div><strong>Stage 3 recovery</strong><p>Task state, attempts and progress events are stored for resumable execution.</p></div><span className="stage-badge">STAGE 3</span></section>
        <footer><span>ProofLoop <span className="footer-dot">Â·</span> Local-first research</span><span>Stage 3 recovery</span></footer>
      </div>
    </main>
  </div>
}

