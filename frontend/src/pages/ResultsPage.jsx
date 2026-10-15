import React, { useEffect, useMemo, useState } from 'react'
import { ArrowDownToLine, ArrowRight, Check, Clipboard, FileSpreadsheet, FileText, Mail, Plus, Search, ShieldAlert } from 'lucide-react'
import { artifactDownloadUrl, createResearchArtifact, getResearchArtifacts } from '../api/client'
import { useResearchEvents } from '../hooks/useResearchEvents'
import StatusBadge from '../components/StatusBadge'

const tabs = ['Overview', 'Findings', 'Sources & Evidence', 'Deliverables']
const terminalStates = ['COMPLETED', 'COMPLETED_WITH_LIMITATIONS', 'FAILED', 'CANCELLED']

export default function ResultsPage({ taskId, navigate }) {
  const { task, sources, findings, connectionError, refreshSaved } = useResearchEvents(taskId)
  const [artifacts, setArtifacts] = useState([])
  const [artifactType, setArtifactType] = useState('spreadsheet')
  const [recipient, setRecipient] = useState('')
  const [drafts, setDrafts] = useState({})
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState('entity')
  const initialTab = new URLSearchParams(window.location.search).get('tab') || 'Overview'
  const [tab, setTab] = useState(tabs.includes(initialTab) ? initialTab : 'Overview')

  useEffect(() => { getResearchArtifacts(taskId).then((data) => setArtifacts(data.artifacts || [])).catch((e) => setError(e.message)) }, [taskId])
  const sourceById = useMemo(() => Object.fromEntries(sources.map((source) => [source.id, source])), [sources])
  const visibleFindings = useMemo(() => findings.filter((finding) => `${finding.entity_name} ${finding.field_name} ${finding.field_value || ''} ${finding.verification_status}`.toLowerCase().includes(search.toLowerCase())).sort((a, b) => String(a[sort === 'field' ? 'field_name' : sort === 'status' ? 'verification_status' : 'entity_name']).localeCompare(String(b[sort === 'field' ? 'field_name' : sort === 'status' ? 'verification_status' : 'entity_name']))), [findings, search, sort])
  const terminal = terminalStates.includes(task?.status)

  async function generate(type) {
    setGenerating(true); setError(''); setNotice('')
    try {
      const artifact = await createResearchArtifact(taskId, { artifact_type: type, recipient: type === 'email' ? recipient || null : null })
      setArtifacts((current) => [artifact, ...current.filter((item) => item.id !== artifact.id)])
      if (type === 'email' && artifact.preview?.body) setDrafts((current) => ({ ...current, [artifact.id]: artifact.preview.body }))
      setNotice(`${type === 'email' ? 'Email draft' : type === 'report' ? 'Research report' : 'Spreadsheet'} created from saved evidence.`)
    } catch (requestError) { setError(requestError.message) }
    finally { setGenerating(false) }
  }

  async function copy(text, label) {
    try { await navigator.clipboard.writeText(text); setNotice(`${label} copied to clipboard.`) }
    catch { setNotice('Clipboard access is unavailable in this browser. Select and copy the text instead.') }
  }

  if (!task && connectionError) return <div className="inline-error" role="alert">Could not load this task: {connectionError}</div>
  if (!task) return <div className="loading-row">Loading saved findings and sources…</div>

  return <section className="results-page page-section">
    <header className="results-header"><div><div className="eyebrow">RESEARCH RESULTS · {task.id.slice(0, 8)}</div><h2>{task.query}</h2><div className="task-meta"><StatusBadge status={task.status} />{task.completed_at && <span>Completed {new Date(task.completed_at).toLocaleString()}</span>}<span>{sources.length} source{sources.length === 1 ? '' : 's'}</span><span>{findings.length} finding{findings.length === 1 ? '' : 's'}</span></div></div><button className="button button-outline button-small" onClick={() => navigate('/research/new')}><Plus size={15} /> New research</button></header>
    {(task.status === 'COMPLETED_WITH_LIMITATIONS' || task.status === 'FAILED' || task.status === 'CANCELLED') && <div className={`task-outcome outcome-${task.status.toLowerCase()}`}><div className="outcome-icon"><ShieldAlert size={19} /></div><div><strong>{task.status === 'FAILED' ? 'This task failed. The saved evidence below may be incomplete.' : task.status === 'CANCELLED' ? 'This task was cancelled. Results show only evidence saved before cancellation.' : 'This task completed with limitations.'}</strong><p>{task.error_summary || task.summary || 'Review the source and verification state of each finding.'}</p></div></div>}
    <div className="results-toolbar"><div className="results-tabs" role="tablist" aria-label="Research results sections">{tabs.map((item) => <button key={item} role="tab" aria-selected={tab === item} className={tab === item ? 'selected' : ''} onClick={() => { setTab(item); const query = item === 'Overview' ? '' : `?tab=${encodeURIComponent(item)}`; window.history.replaceState({}, '', `/research/${taskId}/results${query}`) }}>{item}{item === 'Findings' && <span>{findings.length}</span>}</button>)}</div><button className="text-link" onClick={() => navigate(`/research/${taskId}/progress`)}>View progress history <ArrowRight size={14} /></button></div>

    {tab === 'Overview' && <div className="results-overview results-grid">
      <section className="content-card overview-main"><div className="eyebrow">EXECUTIVE SUMMARY</div><h3>{task.summary ? 'Research summary' : task.plan?.summary || 'Research evidence overview'}</h3><p>{task.summary || 'The task has not recorded a final summary. Use the saved findings and source excerpts below to review the available evidence.'}</p>{task.error_summary && <div className="limitation-note"><ShieldAlert size={16} /><span>{task.error_summary}</span></div>}
        <div className="method-block"><h4>Methodology</h4><p>{task.plan?.summary || 'A structured plan was not saved for this task.'}</p>{task.plan?.topic && <small>Topic: {task.plan.topic}</small>}</div>
        {task.plan?.required_fields?.length > 0 && <div className="method-block"><h4>Requested fields</h4><div className="summary-chips">{task.plan.required_fields.map((field) => <span key={field}>{field}</span>)}</div></div>}
        {task.plan?.actions?.length > 0 && <div className="method-block"><h4>Research plan</h4><ol className="plan-list">{task.plan.actions.map((action) => <li key={action.action_id}><strong>{action.goal}</strong><small>{action.query}</small></li>)}</ol></div>}
        <div className="summary-actions"><button className="button button-outline button-small" onClick={() => copy(`${task.query}\n\n${task.summary || task.error_summary || ''}`, 'Research summary')}><Clipboard size={14} /> Copy summary</button><button className="button button-primary button-small" onClick={() => navigate('/research/new')}><Plus size={14} /> Start new research</button></div>
      </section>
      <aside className="content-card overview-aside"><div className="eyebrow">EVIDENCE SNAPSHOT</div><div className="snapshot-number">{findings.length}<span>findings saved</span></div><div className="snapshot-number">{sources.length}<span>sources recorded</span></div><div className="evidence-legend"><span><i className="legend-supported" /> Source-supported</span><span><i className="legend-unverified" /> Other / unresolved</span></div><button className="text-link" onClick={() => { setTab('Sources & Evidence'); window.history.replaceState({}, '', `/research/${taskId}/results?tab=Sources%20%26%20Evidence`) }}>Inspect sources <ArrowRight size={14} /></button></aside>
    </div>}

    {tab === 'Findings' && <section className="content-card results-panel"><div className="panel-toolbar"><div><div className="eyebrow">EXTRACTED VALUES</div><h3>Findings</h3></div><label className="search-control"><Search size={15} /><input aria-label="Search findings" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search findings" /></label><label className="sort-control">Sort by<select aria-label="Sort findings" value={sort} onChange={(event) => setSort(event.target.value)}><option value="entity">Entity</option><option value="field">Field</option><option value="status">Evidence status</option></select></label></div>
      {visibleFindings.length ? <div className="findings-table-wrap"><table className="findings-table"><thead><tr><th>Entity</th><th>Field</th><th>Extracted value</th><th>Evidence</th><th>Source</th></tr></thead><tbody>{visibleFindings.map((finding) => { const source = sourceById[finding.source_id]; return <tr key={finding.id}><td><strong>{finding.entity_name}</strong></td><td>{finding.field_name}</td><td>{finding.value_status === 'found' ? String(finding.field_value ?? 'Not available') : finding.value_status.replaceAll('_', ' ')}{finding.confidence_notes && <small>{finding.confidence_notes}</small>}</td><td><StatusBadge status={finding.verification_status?.toUpperCase()} />{finding.supporting_excerpt && <blockquote>{finding.supporting_excerpt}</blockquote>}</td><td>{source && (source.source_type === 'user_upload' ? <span>{source.title} · Uploaded</span> : <a href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>)}</td></tr> })}</tbody></table></div> : <div className="empty-inline">{search ? 'No findings match this search.' : 'No findings were saved for this task.'}</div>}</section>}

    {tab === 'Sources & Evidence' && <section className="content-card results-panel"><div className="panel-toolbar"><div><div className="eyebrow">PROVENANCE</div><h3>Sources & supporting evidence</h3></div></div>{sources.length ? <div className="evidence-source-list">{sources.map((source) => <article className="evidence-source-card" key={source.id}><div className="evidence-source-head"><div><span className="source-domain-icon"><FileText size={17} /></span><div>{source.source_type === 'user_upload' ? <strong>{source.title || 'Uploaded material'}</strong> : <a href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}<ArrowRight size={14} /></a>}<small>{source.source_type === 'user_upload' ? 'User-provided text' : source.domain || source.url}{source.retrieved_at ? ` · Retrieved ${new Date(source.retrieved_at).toLocaleString()}` : ' · Discovered, not retrieved'}</small></div></div><StatusBadge status={source.fetch_status?.toUpperCase()} /></div>{source.content_excerpt && <blockquote>{source.content_excerpt}</blockquote>}{source.quality_notes && <p className="source-quality-note">{source.quality_notes}</p>}{findings.filter((finding) => finding.source_id === source.id).length > 0 && <div className="associated-findings"><strong>Associated findings</strong>{findings.filter((finding) => finding.source_id === source.id).map((finding) => <div key={finding.id}><span>{finding.entity_name} · {finding.field_name}</span><StatusBadge status={finding.verification_status?.toUpperCase()} /></div>)}</div>}</article>)}</div> : <div className="empty-inline">No sources were saved for this task.</div>}</section>}

    {tab === 'Deliverables' && <section className="content-card results-panel"><div className="panel-toolbar"><div><div className="eyebrow">FROM SAVED EVIDENCE</div><h3>Deliverables</h3><p>Generate an output from this task’s saved evidence. No new research is started.</p></div></div>
      {!terminal && <div className="inline-note">Deliverables are available after the research task reaches a terminal status.</div>}
      {task.status === 'CANCELLED' && <div className="inline-note">This task was cancelled; artifact generation is disabled.</div>}
      {error && <div className="inline-error" role="alert">{error}</div>}{notice && <div className="success-note" role="status"><Check size={15} />{notice}</div>}
      <div className="deliverable-actions"><button className={`deliverable-action ${artifactType === 'spreadsheet' ? 'chosen' : ''}`} disabled={generating || !terminal || task.status === 'CANCELLED'} onClick={() => { setArtifactType('spreadsheet'); generate('spreadsheet') }}><FileSpreadsheet size={18} /><span><strong>Generate spreadsheet</strong><small>Excel workbook with evidence sheets</small></span></button><button className={`deliverable-action ${artifactType === 'report' ? 'chosen' : ''}`} disabled={generating || !terminal || task.status === 'CANCELLED'} onClick={() => { setArtifactType('report'); generate('report') }}><FileText size={18} /><span><strong>Generate report</strong><small>Markdown summary with source citations</small></span></button><div className="email-action-wrap"><label className="form-label">Email recipient <span className="optional">Optional</span><input aria-label="Email recipient" type="email" value={recipient} onChange={(event) => setRecipient(event.target.value)} placeholder="name@example.com" /></label><button className="deliverable-action" disabled={generating || !terminal || task.status === 'CANCELLED'} onClick={() => { setArtifactType('email'); generate('email') }}><Mail size={18} /><span><strong>Generate email draft</strong><small>Editable draft only · never sent automatically</small></span></button></div></div>
      {generating && <div className="loading-row">Building and validating the selected output…</div>}
      {artifacts.length ? <div className="artifact-list"><h4>Saved deliverables</h4>{artifacts.map((artifact) => <article className="artifact-card" key={artifact.id}><div className="artifact-head"><span className="artifact-kind-icon">{artifact.artifact_type === 'spreadsheet' ? <FileSpreadsheet size={18} /> : artifact.artifact_type === 'email' ? <Mail size={18} /> : <FileText size={18} />}</span><span><strong>{artifact.filename}</strong><small>{artifact.artifact_type} · {artifact.created_at ? new Date(artifact.created_at).toLocaleString() : 'Saved artifact'}</small></span><StatusBadge status={artifact.status} /><a className="button button-outline button-small" href={artifactDownloadUrl(artifact.id)} download={artifact.filename}><ArrowDownToLine size={14} /> Download</a></div>
        {artifact.preview?.summary && <p>{artifact.preview.summary}</p>}{artifact.preview?.task_status && <p>Research status in this artifact: <StatusBadge status={artifact.preview.task_status} /></p>}{artifact.preview?.markdown && <pre className="artifact-preview">{artifact.preview.markdown}</pre>}{artifact.preview?.rows && <div className="artifact-preview-table"><table><thead><tr><th>Entity</th><th>Field</th><th>Value</th><th>Evidence</th></tr></thead><tbody>{artifact.preview.rows.slice(0, 8).map((row, index) => <tr key={`${row.entity}-${row.field}-${index}`}><td>{row.entity}</td><td>{row.field}</td><td>{row.value}</td><td>{row.evidence?.map((evidence, i) => <a key={i} href={evidence.url} target="_blank" rel="noreferrer">{evidence.title || evidence.url}</a>)}</td></tr>)}</tbody></table></div>}{artifact.artifact_type === 'email' && <div className="draft-editor"><label className="form-label">Editable email draft<textarea rows="11" aria-label="Email draft editor" value={drafts[artifact.id] ?? artifact.preview?.body ?? ''} onChange={(event) => setDrafts((current) => ({ ...current, [artifact.id]: event.target.value }))} /></label><div><button className="button button-outline button-small" onClick={() => copy(drafts[artifact.id] ?? artifact.preview?.body ?? '', 'Email draft')}><Clipboard size={14} /> Copy draft</button><span>This remains a draft. Sending is not available.</span></div></div>}</article>)}</div> : !generating && <div className="empty-inline">No deliverables have been generated for this task.</div>}
    </section>}
  </section>
}
