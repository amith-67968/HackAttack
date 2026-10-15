import React, { useEffect, useState } from 'react'
import { ArrowUpRight, FileText, Link2 } from 'lucide-react'
import { getRecentSources } from '../api/client'
import StatusBadge from '../components/StatusBadge'

export default function SourcesPage({ navigate }) {
  const [sources, setSources] = useState([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  useEffect(() => { getRecentSources(100).then((data) => setSources(data.sources || [])).catch((e) => setError(e.message)).finally(() => setLoading(false)) }, [])
  return <section className="page-section">
    <div className="page-intro"><div><div className="eyebrow">SOURCE LIBRARY</div><h2>Sources & evidence</h2><p>Recently retrieved or user-provided source material, linked to its saved research task.</p></div></div>
    {error && <div className="inline-error" role="alert">Could not load sources: {error}</div>}
    {loading ? <div className="loading-row">Loading saved sources…</div> : sources.length ? <div className="source-library">{sources.map((source) => <article className="source-card" key={source.id}><div className="source-card-icon">{source.source_type === 'user_upload' ? <FileText size={18} /> : <Link2 size={18} />}</div><div className="source-card-copy"><div className="source-card-title">{source.source_type === 'user_upload' ? <strong>{source.title || 'Uploaded text'}</strong> : <a href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}<ArrowUpRight size={13} /></a>}<StatusBadge status={source.fetch_status?.toUpperCase()} /></div><p>{source.source_type === 'user_upload' ? 'User-provided text · source excerpt stored with the task' : source.domain || source.url}</p><small>{source.retrieved_at ? `Retrieved ${new Date(source.retrieved_at).toLocaleString()}` : 'Discovered, not yet retrieved'}{source.quality_notes ? ` · ${source.quality_notes}` : ''}</small></div><button className="text-link source-task-link" onClick={() => navigate(`/research/${source.task_id}/results?tab=sources`)}>Open task <ArrowUpRight size={14} /></button></article>)}</div> : <div className="empty-state"><span><Link2 size={20} /></span><div><strong>No sources saved yet</strong><p>Source pages and uploaded text will be listed here after they are associated with a research task.</p></div><button className="text-link" onClick={() => navigate('/research/new')}>Start research <ArrowUpRight size={14} /></button></div>}
  </section>
}
