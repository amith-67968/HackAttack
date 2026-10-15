import React from 'react'
import { ArrowLeft } from 'lucide-react'
import WorkspaceShell from './components/WorkspaceShell'
import HistoryPage from './pages/HistoryPage'
import HomePage from './pages/HomePage'
import ProgressPage from './pages/ProgressPage'
import ResearchSetupPage from './pages/ResearchSetupPage'
import ResultsPage from './pages/ResultsPage'
import SourcesPage from './pages/SourcesPage'
import { navigate, usePathname } from './router'
import './styles.css'

export default function App() {
  const route = usePathname()
  const pathname = route.split('?')[0]

  if (pathname === '/') return <HomePage navigate={navigate} />

  let title = 'Workspace'
  let subtitle = 'Research workspace'
  let page = <div className="not-found"><h2>We couldn’t find that page.</h2><button className="button button-primary" onClick={() => navigate('/')}><ArrowLeft size={15} /> Back to overview</button></div>
  const taskMatch = pathname.match(/^\/research\/([^/]+)\/(progress|results)$/)
  if (pathname === '/research/new') {
    title = 'New research'; subtitle = 'Configure a research run'; page = <ResearchSetupPage navigate={navigate} />
  } else if (pathname === '/research') {
    title = 'Research runs'; subtitle = 'Saved research history'; page = <HistoryPage navigate={navigate} />
  } else if (pathname === '/sources') {
    title = 'Sources & evidence'; subtitle = 'Saved source library'; page = <SourcesPage navigate={navigate} />
  } else if (taskMatch?.[2] === 'progress') {
    title = 'Live research'; subtitle = 'Task progress'; page = <ProgressPage taskId={taskMatch[1]} navigate={navigate} />
  } else if (taskMatch?.[2] === 'results') {
    title = 'Research results'; subtitle = 'Findings and deliverables'; page = <ResultsPage taskId={taskMatch[1]} navigate={navigate} />
  }

  return <WorkspaceShell navigate={navigate} currentPath={pathname} title={title} subtitle={subtitle}>{page}</WorkspaceShell>
}
