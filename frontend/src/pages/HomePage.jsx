import React, { useEffect, useState } from 'react'
import { ArrowRight, ArrowUpRight, BookOpen, CheckCheck, CircleHelp, Clock3, FileText, Search, ShieldCheck, Sparkles, Waves, Lightbulb } from 'lucide-react'
import { getHealth, getRecentResearch } from '../api/client'
import StatusBadge from '../components/StatusBadge'

const examples = [
  { icon: Search, title: 'Find internships', text: 'Compare current internships by stipend, eligibility, skills and deadline.' },
  { icon: BookOpen, title: 'Compare colleges', text: 'Review engineering colleges with clear ranking criteria and sources.' },
  { icon: ShieldCheck, title: 'Explore local services', text: 'Compare published services, prices and contact details in your area.' },
  { icon: Lightbulb, title: 'Investigate a question', text: 'Follow a topic through trustworthy pages, findings and cited evidence.' },
]

function formatDate(value) {
  if (!value) return 'Recently updated'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? 'Recently updated' : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

export default function HomePage({ navigate }) {
  const [health, setHealth] = useState(null)
  const [healthError, setHealthError] = useState('')
  const [tasks, setTasks] = useState([])
  const [historyError, setHistoryError] = useState('')
  const [loadingTasks, setLoadingTasks] = useState(true)

  useEffect(() => {
    getHealth().then(setHealth).catch((error) => setHealthError(error.message))
    getRecentResearch(5).then((response) => setTasks(response.tasks || []))
      .catch((error) => setHistoryError(error.message)).finally(() => setLoadingTasks(false))

    const header = document.querySelector('.marketing-nav')
    const onScroll = () => {
      if (!header) return
      if (window.scrollY > 16) header.classList.add('is-scrolled')
      else header.classList.remove('is-scrolled')
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })

    const reveals = document.querySelectorAll('.reveal')
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('in')
            io.unobserve(entry.target)
          }
        })
      },
      { threshold: 0.12, rootMargin: '0px 0px -12px 0px' }
    )
    reveals.forEach((el) => io.observe(el))

    return () => {
      window.removeEventListener('scroll', onScroll)
      io.disconnect()
    }
  }, [])
  useEffect(() => {
    const preview = document.getElementById('heroPreview')
    const visual = preview?.parentElement
    if (visual) {
      const applyTilt = (clientX, clientY) => {
        const rect = visual.getBoundingClientRect()
        const x = (clientX - rect.left) / rect.width - 0.5
        const y = (clientY - rect.top) / rect.height - 0.5
        const rotateX = -y * 8
        preview.style.transform = `rotateX(${-y*8}deg) rotateY(${x*10}deg) translateZ(${Math.abs(x+y)*6}px)`
        preview.style.transform = `perspective(1000px) rotateX(${rotateX}deg) rotateY(${rotateY}deg) translateZ(10px)`
      }
      const onMove = (event) => applyTilt(event.clientX, event.clientY)
      const onTouchMove = (event) => {
        const touch = event.touches[0]
      visual.addEventListener('touchmove', (e)=>{ const t=e.touches[0]; const rect=visual.getBoundingClientRect(); const x=(t.clientX-rect.left)/rect.width-0.5; const y=(t.clientY-rect.top)/rect.height-0.5; preview.style.transform=`rotateX(${-y*6}deg) rotateY(${x*8}deg)` }, {passive:true})
      }
      const reset = () => { preview.style.transform = 'rotateX(0deg) rotateY(0deg) translateZ(0px)' }
      visual.addEventListener('mousemove', onMove)
      visual.addEventListener('mouseleave', reset)
      visual.addEventListener('touchmove', onTouchMove, { passive: true })
      visual.addEventListener('touchend', reset)
      return () => {
        visual.removeEventListener('mousemove', onMove)
        visual.removeEventListener('mouseleave', reset)
        visual.removeEventListener('touchmove', onTouchMove)
        visual.removeEventListener('touchend', reset)
      }
    }
  }, [])


  const components = health?.components || {}
  const serviceRows = [
    { name: 'ProofLoop API', status: components.api?.status === 'available' ? 'Available' : healthError ? 'Unavailable' : 'Checking', ok: components.api?.status === 'available' },
    { name: 'Ollama', status: components.ollama?.model_available ? 'Model ready' : components.ollama?.connected ? 'Model missing' : 'Not connected', ok: Boolean(components.ollama?.model_available) },
    { name: 'Supabase', status: components.supabase?.status === 'configured' ? 'Configured' : 'Not configured', ok: components.supabase?.status === 'configured' },
    { name: 'Tavily + Firecrawl', status: components.tavily?.status === 'configured' && components.firecrawl?.status === 'configured' ? 'Configured' : 'Setup needed', ok: components.tavily?.status === 'configured' && components.firecrawl?.status === 'configured' },
  ]

  return <div className="landing-page">
    <header className="marketing-nav">
      <a href="/" className="brand-lockup" onClick={(event) => { event.preventDefault(); navigate('/') }} aria-label="ProofLoop home"><span className="brand-symbol"><Waves size={19} /></span><span>proofloop</span></a>
      <nav aria-label="Main navigation"><a href="#how-it-works">How it works</a><a href="#recent">Recent research</a></nav>
      <button className="button button-outline button-small" onClick={() => navigate('/research')}>Research history <ArrowUpRight size={15} /></button>
    </header>

    <main>
      <section className="hero-section">
        <div className="hero-copy">
          <div className="eyebrow"><Sparkles size={14} /> Evidence-led research workspace</div>
          <h1>Research that<br /><span>shows its work.</span></h1>
          <p>ProofLoop turns a clear question into a sourced, reviewable answer. Follow each step, see where every finding came from, and keep useful work when a task needs recovery.</p>
          <div className="hero-actions"><button className="button button-primary" onClick={() => navigate('/research/new')}>Start researching <ArrowRight size={17} /></button><button className="button button-quiet" onClick={() => navigate('/research')}>View research history</button></div>
          <div className="trust-note"><ShieldCheck size={15} /> Sources and evidence stay visible alongside each finding.</div>
        </div>
        <div className="hero-visual" aria-label="Illustration of a source-backed research finding">
          <div className="hero-orb orb-indigo" />
          <div className="hero-orb orb-sky" />
          <div className="visual-orbit orbit-one" /><div className="visual-orbit orbit-two" />
          <div className="research-preview" id="heroPreview">
            <div className="preview-top"><div><span className="preview-kicker">RESEARCH NOTE</span><strong>Evidence, connected.</strong></div><span className="preview-icon"><CheckCheck size={18} /></span></div>
            <div className="preview-query"><span>QUESTION</span><p>What does the source actually say?</p></div>
            <div className="preview-finding"><div className="finding-check"><CheckCheck size={14} /></div><div><span>SUPPORTED FINDING</span><strong>A clear value, with context.</strong><p>Extracted values remain attached to their supporting excerpt.</p></div></div>
            <div className="preview-source"><span className="source-favicon">Ã¢â€ â€”</span><div><strong>Original source</strong><small>URL Ã‚Â· retrieval time Ã‚Â· excerpt</small></div><ArrowUpRight size={15} /></div>
          </div>
          <div className="floating-chip chip-sources"><span /> Source-linked</div>
          <div className="floating-chip chip-progress"><Clock3 size={14} /> Saved progress</div>
        </div>
      </section>

      <section className="service-strip" aria-label="Current system status">
        <div className="service-heading"><span className="live-indicator" /> Local system status</div>
        {serviceRows.map((service) => <div className="service-item" key={service.name}><span className={`service-dot ${service.ok ? 'is-ready' : ''}`} /><span>{service.name}</span><strong>{service.status}</strong></div>)}
        {healthError && <p className="service-error" role="status">Health check unavailable: {healthError}</p>}
      </section>

      <section className="section examples-section">
        <div className="section-intro"><div><div className="eyebrow">A GOOD PLACE TO BEGIN</div><h2>What are you trying to find out?</h2><p>Start with a question. Refine the scope and output before your task begins.</p></div><button className="text-link" onClick={() => navigate('/research/new')}>Build a research request <ArrowRight size={15} /></button></div>
        <div className="example-grid">{examples.map(({ icon: Icon, title, text }) => <button className="example-card reveal" key={title} onClick={() => navigate(`/research/new?prompt=${encodeURIComponent(text)}`)}><span className="example-icon"><Icon size={18} /></span><strong>{title}</strong><p>{text}</p><ArrowUpRight className="example-arrow" size={16} /></button>)}</div>
      </section>

      <section className="how-section" id="how-it-works">
        <div className="section-intro"><div><div className="eyebrow">FROM QUESTION TO ANSWER</div><h2>Keep the path to the answer in view.</h2></div><p className="section-aside">Research is more useful when you can inspect its sources, its gaps and the work behind the final output.</p></div>
        <div className="process-grid"><article className="reveal"><span>01</span><h3>Research</h3><p>Build a plan, search focused queries and read relevant pages.</p></article><article className="reveal"><span>02</span><h3>Verify</h3><p>Keep findings tied to excerpts, sources and honest evidence states.</p></article><article className="reveal"><span>03</span><h3>Deliver</h3><p>Turn saved findings into a spreadsheet, report or editable email draft.</p></article></div>
        <div className="principles-row"><div className="reveal"><ShieldCheck size={19} /><strong>Evidence stays attached</strong><p>Open the original source and inspect the supporting text.</p></div><div><RefreshIcon /><strong>Progress can recover</strong><p>Saved task and step state helps resume unfinished work.</p></div><div className="reveal"><FileText size={19} /><strong>Outputs use saved work</strong><p>Generate a format from evidence already collected.</p></div></div>
      </section>

      <section className="recent-section" id="recent">
        <div className="section-intro"><div><div className="eyebrow">YOUR WORKSPACE</div><h2>Recent research</h2><p>Tasks shown here come from your saved research history.</p></div><button className="text-link" onClick={() => navigate('/research')}>All research runs <ArrowRight size={15} /></button></div>
        {historyError ? <div className="inline-error" role="status">Could not load saved tasks: {historyError}</div> : loadingTasks ? <div className="loading-row">Loading saved researchâ€¦</div> : tasks.length ? <div className="recent-list">{tasks.map((task) => <button className="recent-task reveal" key={task.id} onClick={() => navigate(`/research/${task.id}/${['COMPLETED', 'COMPLETED_WITH_LIMITATIONS', 'FAILED', 'CANCELLED'].includes(task.status) ? 'results' : 'progress'}`)}><span className="recent-task-icon"><FileText size={17} /></span><span className="recent-task-main"><strong>{task.query}</strong><small>{formatDate(task.updated_at || task.created_at)}</small></span><StatusBadge status={task.status} /><ArrowRight size={16} className="recent-arrow" /></button>)}</div> : <div className="empty-state"><span><CircleHelp size={20} /></span><div><strong>No research runs yet</strong><p>Your saved tasks will show here after you start researching.</p></div><button className="text-link" onClick={() => navigate('/research/new')}>Start the first one <ArrowRight size={14} /></button></div>}
      </section>
    </main>
    <footer className="site-footer"><span className="brand-lockup"><span className="brand-symbol"><Waves size={15} /></span>proofloop</span><span>Local-first research Â· Source-backed by design</span><button className="text-link" onClick={() => navigate('/research/new')}>Start a question <ArrowRight size={14} /></button></footer>
  </div>
}

function RefreshIcon() {
  return <span className="refresh-symbol" aria-hidden="true">â†»</span>
}
