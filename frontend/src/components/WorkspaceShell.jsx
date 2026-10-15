import React from 'react'
import { Activity, BookOpen, CircleHelp, FilePlus2, Waves } from 'lucide-react'

export default function WorkspaceShell({ children, navigate, currentPath, title, subtitle }) {
  const match = currentPath.match(/^\/research\/([^/]+)/)
  const taskId = match?.[1]
  const active = currentPath.startsWith('/research/new') ? 'new'
    : currentPath.startsWith('/sources') ? 'sources'
      : currentPath.startsWith('/research/') ? 'task' : 'runs'
  const items = [
    { id: 'overview', label: 'Overview', icon: Activity, path: '/' },
    { id: 'runs', label: 'Research runs', icon: BookOpen, path: '/research' },
    { id: 'sources', label: 'Sources & evidence', icon: FilePlus2, path: '/sources' },
  ]

  return <div className="workspace-layout">
    <aside className="workspace-sidebar">
      <a href="/" className="brand-lockup" onClick={(event) => { event.preventDefault(); navigate('/') }}><span className="brand-symbol"><Waves size={19} /></span><span>proofloop</span></a>
      <div className="workspace-label">WORKSPACE</div>
      <nav className="workspace-nav" aria-label="Workspace navigation">
        {items.map(({ id, label, icon: Icon, path }) => <button key={id} className={`workspace-nav-item ${(active === id || (id === 'runs' && active === 'new')) ? 'active' : ''}`} onClick={() => navigate(path)}><Icon size={17} />{label}</button>)}
      </nav>
      <button className="sidebar-new-button" onClick={() => navigate('/research/new')}><FilePlus2 size={16} /> New research</button>
      <div className="sidebar-bottom"><span className="workspace-avatar">P</span><span><strong>Local workspace</strong><small>Private to this machine</small></span><CircleHelp size={16} /></div>
    </aside>
    <main className="workspace-main">
      <header className="workspace-topbar"><div><div className="workspace-breadcrumb">ProofLoop <span>/</span> {title}</div><h1>{subtitle || title}</h1></div><button className="button button-primary button-small" onClick={() => navigate('/research/new')}><FilePlus2 size={15} /> New research</button></header>
      {taskId && <div className="task-context-bar"><span>Viewing task <strong>{taskId.slice(0, 8)}</strong></span><button className="text-link" onClick={() => navigate(`/research/${taskId}/progress`)}>Live progress <Activity size={14} /></button></div>}
      <div className="workspace-content">{children}</div>
    </main>
  </div>
}
