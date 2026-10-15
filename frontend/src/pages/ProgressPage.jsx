import React, { useMemo, useState } from 'react'
import { Activity, ArrowRight, Check, Clock3, ExternalLink, RefreshCw, RotateCcw, ShieldAlert, XCircle } from 'lucide-react'
import { cancelResearch, resumeResearch } from '../api/client'
import { useResearchEvents } from '../hooks/useResearchEvents'
import StatusBadge from '../components/StatusBadge'

function eventMessage(event) {
  const { type, data = {} } = event
  if (type === 'task_status') return data.message || `Task status changed to ${data.status || 'updated'}.`
  if (type === 'step_started') return data.title || `Started ${data.action_type || 'a research step'}.`
  if (type === 'tool_call_started') return `Calling ${data.tool || 'a research tool'}${data.query ? ` · ${data.query}` : ''}.`
  if (type === 'tool_call_finished') return `${data.tool || 'Tool'} ${String(data.status || 'finished').toLowerCase()}.`
  if (type === 'source_discovered') return data.title || data.url || 'A source was discovered.'
  if (type === 'finding_extracted') return `Extracted ${data.count || 'some'} field(s)${data.entity_name ? ` for ${data.entity_name}` : ''}.`
  if (type === 'verification_result') return `Evidence check: ${data.status || data.result || 'recorded'}.`
  if (type === 'retry_started') return `Retry ${data.next_attempt || data.attempt || ''} of ${data.max_attempts || 'the configured limit'} after ${data.error_classification || 'a temporary error'}.`
  if (type === 'step_failed' || type === 'task_error') return data.message || 'The operation failed.'
  if (type === 'task_resumed' || type === 'recovery_started') return 'Recovery started from saved task state.'
  if (type === 'task_cancel_requested') return 'Cancellation requested; the current safe operation will finish first.'
  if (type === 'worker_interrupted') return 'The worker stopped. Saved progress is available for recovery.'
  if (type === 'finding_rejected') return `${data.count || 'Some'} finding(s) were excluded after evidence validation.`
  if (type === 'artifact_generated') return `Created ${data.artifact_type || 'a deliverable'} from saved evidence.`
  return type.replaceAll('_', ' ')
}

function elapsed(startedAt, updatedAt) {
  if (!startedAt) return null
  const from = new Date(startedAt).valueOf()
  const to = updatedAt ? new Date(updatedAt).valueOf() : Date.now()
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null
  const seconds = Math.max(0, Math.floor((to - from) / 1000))
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

export default function ProgressPage({ taskId, navigate }) {
  const { events, task, sources, findings, steps, connectionError, reconnecting, refreshSaved, reconnect } = useResearchEvents(taskId)
  const [actionError, setActionError] = useState('')
  const [working, setWorking] = useState(false)
  const terminal = ['COMPLETED', 'COMPLETED_WITH_LIMITATIONS', 'FAILED', 'CANCELLED'].includes(task?.status)
  const visibleEvents = useMemo(() => events, [events])

  async function resume() {
    setWorking(true); setActionError('')
    try { await resumeResearch(taskId); reconnect(); await refreshSaved() }
    catch (error) { setActionError(error.message) }
    finally { setWorking(false) }
  }
  async function cancel() {
    if (!window.confirm('Request cancellation for this research task?')) return
    setWorking(true); setActionError('')
    try { await cancelResearch(taskId); await refreshSaved() }
    catch (error) { setActionError(error.message) }
    finally { setWorking(false) }
  }

  const timeline = steps.map((step) => ({ id: step.id || step.action_key, title: step.title || step.action_type, status: step.status, detail: step.last_error || step.expected_result?.description }))
  return <section className="progress-page page-section">
    {!task && !connectionError && <div className="loading-row">Restoring saved task state…</div>}
    {connectionError && <div className="inline-error" role="alert"><strong>{reconnecting ? 'Reconnecting to saved progress' : 'Could not refresh task state'}</strong><span>{connectionError}</span><button className="text-link" onClick={() => { refreshSaved().catch((error) => setActionError(error.message)); reconnect() }}>Reconnect <RotateCcw size={14} /></button></div>}
    {task && <>
      <div className="progress-header"><div><div className="eyebrow">RESEARCH RUN · {task.id.slice(0, 8)}</div><h2>{task.query}</h2><div className="task-meta"><StatusBadge status={task.status} />{task.started_at && <span><Clock3 size={14} /> Started {new Date(task.started_at).toLocaleString()}</span>}{elapsed(task.started_at, task.updated_at) && <span>{elapsed(task.started_at, task.updated_at)} elapsed</span>}</div></div><button className="button button-outline button-small" onClick={() => navigate('/research')}>Back to research runs</button></div>
      <div className={`task-outcome outcome-${String(task.status).toLowerCase()}`}><div className="outcome-icon">{task.status === 'FAILED' ? <XCircle size={19} /> : task.status === 'COMPLETED' ? <Check size={19} /> : task.status === 'COMPLETED_WITH_LIMITATIONS' ? <ShieldAlert size={19} /> : <Activity size={19} />}</div><div><strong>{task.status === 'FAILED' ? 'This task did not complete.' : task.status === 'COMPLETED_WITH_LIMITATIONS' ? 'Completed with limitations.' : task.status === 'COMPLETED' ? 'Research completed.' : task.status === 'CANCELLED' ? 'Research was cancelled.' : 'Research is in progress.'}</strong><p>{task.summary || task.error_summary || (terminal ? 'Saved task state and evidence are available to review.' : 'Progress is based on persisted steps and events; no estimated percentage is shown.')}</p>{task.error_summary && task.summary && <p className="outcome-error">{task.error_summary}</p>}</div></div>

      <div className="progress-stats"><div><span>Sources found</span><strong>{sources.length}</strong></div><div><span>Saved findings</span><strong>{findings.length}</strong></div><div><span>Recorded steps</span><strong>{steps.length}</strong></div></div>

      <div className="progress-columns">
        <section className="content-card"><div className="card-heading"><div><div className="eyebrow">PERSISTED WORK</div><h3>Research steps</h3></div><button className="icon-button" aria-label="Refresh task state" onClick={() => refreshSaved().catch((error) => setActionError(error.message))}><RotateCcw size={16} /></button></div>
          {timeline.length ? <ol className="step-timeline">{timeline.map((step) => <li className="step-timeline-item" key={step.id}><span className={`step-node step-${String(step.status).toLowerCase()}`} /> <div><div className="timeline-title"><strong>{step.title}</strong><StatusBadge status={step.status} /></div>{step.detail && <p>{step.detail}</p>}</div></li>)}</ol> : <div className="empty-inline">The plan has not recorded any steps yet.</div>}
        </section>
        <section className="content-card"><div className="card-heading"><div><div className="eyebrow">ACTIVITY</div><h3>Live event history</h3></div><span className={`connection-pill ${reconnecting ? 'is-reconnecting' : ''}`}><i />{reconnecting ? 'Reconnecting' : terminal ? 'Saved history' : 'Listening'}</span></div>
          {visibleEvents.length ? <ol className="activity-list">{visibleEvents.map((event) => <li key={`${event.id}-${event.type}`} className={['task_error', 'step_failed'].includes(event.type) ? 'activity-error' : ''}><time>{event.data.created_at ? new Date(event.data.created_at).toLocaleTimeString() : ''}</time><div><strong>{event.type.replaceAll('_', ' ')}</strong><p>{eventMessage(event)}</p></div></li>)}</ol> : <div className="empty-inline">No persisted events have arrived yet.</div>}
          {sources.length > 0 && <div className="mini-source-list"><h4>Discovered sources</h4>{sources.slice(0, 5).map((source) => <div key={source.id}>{source.source_type === 'user_upload' ? <span>{source.title || 'Uploaded text'}</span> : <a href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}<ExternalLink size={12} /></a>}<StatusBadge status={source.fetch_status?.toUpperCase()} /></div>)}{sources.length > 5 && <button className="text-link" onClick={() => navigate(`/research/${taskId}/results?tab=sources`)}>View all {sources.length} sources <ArrowRight size={14} /></button>}</div>}
        </section>
      </div>
      {actionError && <div className="inline-error" role="alert">{actionError}</div>}
      <div className="progress-actions">{task.resume_eligible && <button className="button button-outline" disabled={working} onClick={resume}><RefreshCw size={15} className={working ? 'spin' : ''} /> Resume saved work</button>}{task.cancel_eligible && <button className="button button-danger-quiet" disabled={working} onClick={cancel}><XCircle size={15} /> Request cancellation</button>}{terminal && <button className="button button-primary" onClick={() => navigate(`/research/${taskId}/results`)}>Open results <ArrowRight size={16} /></button>}</div>
    </>}
  </section>
}
