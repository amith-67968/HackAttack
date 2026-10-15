import React, { useEffect, useState } from 'react'
import { ArrowRight, FileText, Search } from 'lucide-react'
import { getRecentResearch } from '../api/client'
import StatusBadge from '../components/StatusBadge'

export default function HistoryPage({ navigate }) {
  const [tasks, setTasks] = useState([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  useEffect(() => { getRecentResearch(50).then((data) => setTasks(data.tasks || [])).catch((e) => setError(e.message)).finally(() => setLoading(false)) }, [])
  const visible = tasks.filter((task) => task.query.toLowerCase().includes(filter.toLowerCase()))
  return <section className="page-section">
    <div className="page-intro"><div><div className="eyebrow">WORKSPACE HISTORY</div><h2>Research runs</h2><p>Open a saved task to review its live state, evidence or deliverables.</p></div><label className="search-control"><Search size={16} /><input aria-label="Search research runs" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Search your research" /></label></div>
    {error && <div className="inline-error" role="alert">Could not load research history: {error}</div>}
    {loading ? <div className="loading-row">Loading saved tasks…</div> : visible.length ? <div className="history-table">{visible.map((task) => <button className="history-row" key={task.id} onClick={() => navigate(`/research/${task.id}/${['COMPLETED', 'COMPLETED_WITH_LIMITATIONS', 'FAILED', 'CANCELLED'].includes(task.status) ? 'results' : 'progress'}`)}><span className="history-icon"><FileText size={17} /></span><span className="history-main"><strong>{task.query}</strong><small>{task.created_at ? new Date(task.created_at).toLocaleString() : 'Date unavailable'}{task.summary ? ` · ${task.summary}` : ''}</small></span><StatusBadge status={task.status} /><ArrowRight size={16} className="history-arrow" /></button>)}</div> : <div className="empty-state"><span><Search size={20} /></span><div><strong>{filter ? 'No matching research' : 'No research saved yet'}</strong><p>{filter ? 'Try a different search term.' : 'When you start a task, it will appear here with its current status.'}</p></div></div>}
  </section>
}
