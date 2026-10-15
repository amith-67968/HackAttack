import React, { useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, FileText, Link2, LoaderCircle, Plus, Upload, X } from 'lucide-react'
import { createResearch } from '../api/client'

const MAX_FILE_BYTES = 50_000
const MAX_FILES = 4
const MAX_URLS = 5
const validUrl = (value) => {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
  } catch { return false }
}
const fileExtension = (name) => name.toLowerCase().slice(name.lastIndexOf('.'))

export default function ResearchSetupPage({ navigate }) {
  const [step, setStep] = useState(0)
  const [query, setQuery] = useState(() => new URLSearchParams(window.location.search).get('prompt') || '')
  const [targetCount, setTargetCount] = useState('')
  const [location, setLocation] = useState('')
  const [domain, setDomain] = useState('')
  const [fields, setFields] = useState('')
  const [depth, setDepth] = useState('standard')
  const [outputType, setOutputType] = useState('')
  const [materials, setMaterials] = useState([])
  const [filesError, setFilesError] = useState('')
  const [urls, setUrls] = useState([])
  const [urlDraft, setUrlDraft] = useState('')
  const [urlError, setUrlError] = useState('')
  const [dragging, setDragging] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [readingFiles, setReadingFiles] = useState(false)
  const fileInput = useRef(null)

  const stepNames = ['Research goal', 'Input materials', 'Output settings']
  const normalizedFields = useMemo(() => fields.split(/[\n,]/).map((field) => field.trim()).filter(Boolean), [fields])

  async function addFiles(fileList) {
    setFilesError('')
    const incoming = Array.from(fileList || [])
    if (!incoming.length) return
    const accepted = []
    const rejected = []
    let slots = MAX_FILES - materials.length
    setReadingFiles(true)
    for (const file of incoming) {
      const extension = fileExtension(file.name)
      if (!['.txt', '.md', '.markdown'].includes(extension)) { rejected.push(`${file.name}: only TXT and Markdown files are supported.`); continue }
      if (file.size > MAX_FILE_BYTES) { rejected.push(`${file.name}: files must be 50 KB or smaller.`); continue }
      if (slots <= 0) { rejected.push(`Only ${MAX_FILES} files can be added.`); continue }
      try {
        const content = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
        if (!content.trim()) { rejected.push(`${file.name}: the file is empty.`); continue }
        accepted.push({ filename: file.name, content, size: file.size, type: extension === '.txt' ? 'Text' : 'Markdown' })
        slots -= 1
      } catch { rejected.push(`${file.name}: the file must contain valid UTF-8 text.`) }
    }
    setMaterials((current) => [...current, ...accepted].slice(0, MAX_FILES))
    setReadingFiles(false)
    setFilesError(rejected.join(' '))
    if (fileInput.current) fileInput.current.value = ''
  }

  function addUrl() {
    setUrlError('')
    const value = urlDraft.trim()
    if (!validUrl(value)) { setUrlError('Enter a valid public HTTP or HTTPS webpage URL.'); return }
    if (urls.includes(value)) { setUrlError('That URL is already in the list.'); return }
    if (urls.length >= MAX_URLS) { setUrlError(`Add up to ${MAX_URLS} reference URLs.`); return }
    setUrls((current) => [...current, value])
    setUrlDraft('')
  }

  function continueStep() {
    setError('')
    if (step === 0 && query.trim().length < 5) { setError('Add a research question with at least five characters.'); return }
    if (step === 1 && urlDraft.trim()) { setUrlError('Add or remove the URL in the field before continuing.'); return }
    setStep((current) => Math.min(current + 1, 2))
  }

  async function startResearch(event) {
    event.preventDefault()
    if (query.trim().length < 5) { setError('Add a research question with at least five characters.'); setStep(0); return }
    if (!outputType) { setError('Choose an output format before starting research.'); return }
    if (submitting) return
    setSubmitting(true)
    setError('')
    try {
      const result = await createResearch({
        query: query.trim(), output_type: outputType, research_depth: depth,
        target_count: targetCount ? Number(targetCount) : null,
        options: { location: location.trim() || null, domain: domain.trim() || null, required_fields: normalizedFields, reference_urls: urls },
        input_materials: materials.map(({ filename, content }) => ({ filename, content })),
      })
      if (!result?.task_id) throw new Error('The API did not return a task ID.')
      window.localStorage.setItem('proofloop-current-task', result.task_id)
      navigate(`/research/${result.task_id}/progress`)
    } catch (submitError) { setError(submitError.message) }
    finally { setSubmitting(false) }
  }

  return <section className="setup-page page-section">
    <div className="setup-heading"><div><div className="eyebrow">NEW RESEARCH RUN</div><h2>Give your question a clear starting point.</h2><p>Set the scope, add reference material and choose how you’d like to review the results.</p></div></div>
    <div className="wizard-progress" aria-label="Research setup steps">{stepNames.map((name, index) => <div className={`wizard-step ${step === index ? 'current' : ''} ${step > index ? 'done' : ''}`} key={name}><span>{step > index ? <Check size={14} /> : index + 1}</span><strong>{name}</strong></div>)}</div>

    <form className="setup-card" onSubmit={step === 2 ? startResearch : (event) => { event.preventDefault(); continueStep() }}>
      {step === 0 && <div className="setup-panel">
        <div className="panel-heading"><span className="panel-index">01</span><div><h3>What do you want to learn?</h3><p>Be specific about the topic and what a useful answer includes.</p></div></div>
        <label className="form-label" htmlFor="research-query">Research question</label>
        <textarea id="research-query" className="large-textarea" value={query} onChange={(event) => setQuery(event.target.value)} maxLength={4000} placeholder="For example: Find physiotherapy centres in Bengaluru and compare their published consultation fees." />
        <div className="field-grid">
          <label className="form-label">Target number <span className="optional">Optional</span><input type="number" min="1" max="100" value={targetCount} onChange={(event) => setTargetCount(event.target.value)} placeholder="No target" /></label>
          <label className="form-label">Location <span className="optional">Optional</span><input value={location} onChange={(event) => setLocation(event.target.value)} placeholder="e.g. Bengaluru" /></label>
          <label className="form-label">Preferred website domain <span className="optional">Optional</span><input value={domain} onChange={(event) => setDomain(event.target.value)} placeholder="example.org" /></label>
          <label className="form-label">Research depth<select value={depth} onChange={(event) => setDepth(event.target.value)}><option value="standard">Standard · focused source coverage</option><option value="deep">Deep · broader search within configured limits</option></select></label>
        </div>
        <label className="form-label requested-fields">Fields or details to extract <span className="optional">Optional · comma or line separated</span><textarea rows="2" value={fields} onChange={(event) => setFields(event.target.value)} placeholder="Consultation fee, eligibility, application deadline" /></label>
      </div>}

      {step === 1 && <div className="setup-panel">
        <div className="panel-heading"><span className="panel-index">02</span><div><h3>Add useful context</h3><p>Reference webpages and small text files will be saved with the task and used as evidence.</p></div></div>
        <div className={`drop-zone ${dragging ? 'drag-over' : ''}`} onDragOver={(event) => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); addFiles(event.dataTransfer.files) }}>
          <span className="drop-icon"><Upload size={20} /></span><strong>{readingFiles ? 'Reading your files…' : 'Drop text files here'}</strong><p>TXT and Markdown only · up to 50 KB each · {MAX_FILES} files maximum</p>
          <input ref={fileInput} type="file" accept=".txt,.md,.markdown,text/plain,text/markdown" multiple hidden onChange={(event) => addFiles(event.target.files)} />
          <button type="button" className="button button-outline button-small" disabled={readingFiles || materials.length >= MAX_FILES} onClick={() => fileInput.current?.click()}>{readingFiles ? <LoaderCircle className="spin" size={15} /> : <Upload size={15} />} Choose files</button>
          <small className="format-note">PDF and DOCX are not offered because this backend does not parse them yet.</small>
        </div>
        {filesError && <p className="field-error" role="alert">{filesError}</p>}
        {materials.length > 0 && <div className="material-list" aria-label="Selected input files">{materials.map((file) => <article className="material-row" key={`${file.filename}-${file.size}`}><span className="material-icon"><FileText size={17} /></span><span><strong>{file.filename}</strong><small>{file.type} · {file.size.toLocaleString()} bytes · Ready</small></span><button type="button" className="icon-button" aria-label={`Remove ${file.filename}`} onClick={() => setMaterials((current) => current.filter((item) => item !== file))}><X size={16} /></button></article>)}</div>}
        <div className="url-entry"><label className="form-label" htmlFor="reference-url">Reference webpages <span className="optional">Up to {MAX_URLS}</span></label><div className="url-input-row"><span><Link2 size={16} /></span><input id="reference-url" type="url" value={urlDraft} onChange={(event) => { setUrlDraft(event.target.value); setUrlError('') }} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addUrl() } }} placeholder="https://example.org/research-page" /><button type="button" className="button button-outline button-small" onClick={addUrl} disabled={!urlDraft.trim() || urls.length >= MAX_URLS}><Plus size={15} /> Add URL</button></div>{urlError && <p className="field-error" role="alert">{urlError}</p>}</div>
        {urls.length > 0 && <ul className="url-list">{urls.map((url) => <li key={url}><span><Link2 size={14} /><span>{url}</span></span><button type="button" className="icon-button" aria-label={`Remove ${url}`} onClick={() => setUrls((current) => current.filter((item) => item !== url))}><X size={15} /></button></li>)}</ul>}
      </div>}

      {step === 2 && <div className="setup-panel">
        <div className="panel-heading"><span className="panel-index">03</span><div><h3>Choose a useful output</h3><p>All deliverables are generated from the saved research evidence after the run.</p></div></div>
        <div className="output-options" role="radiogroup" aria-label="Output format" aria-required="true">{[
          ['auto', 'Choose for me', 'Use the format implied by my request.'],
          ['spreadsheet', 'Spreadsheet', 'Structured findings and source links in XLSX.'],
          ['report', 'Research report', 'A Markdown report with evidence and limitations.'],
          ['email', 'Email draft', 'An editable summary draft. Nothing is sent.'],
        ].map(([value, title, description]) => <label key={value} className={`output-option ${outputType === value ? 'selected' : ''}`}><input type="radio" name="output" value={value} checked={outputType === value} onChange={() => { setOutputType(value); setError('') }} /><span className="radio-mark" /><span><strong>{title}</strong><small>{description}</small></span></label>)}</div>
        <div className="request-summary"><div className="eyebrow">{outputType ? 'READY TO START' : 'OUTPUT FORMAT REQUIRED'}</div><p className="summary-query">“{query.trim()}”</p><div className="summary-chips"><span>{depth} depth</span><span>{!outputType ? 'Choose an output format' : outputType === 'auto' ? 'Automatic format' : outputType}</span>{location && <span>{location}</span>}{targetCount && <span>{targetCount} target results</span>}{urls.length > 0 && <span>{urls.length} reference URL{urls.length === 1 ? '' : 's'}</span>}{materials.length > 0 && <span>{materials.length} text file{materials.length === 1 ? '' : 's'}</span>}</div></div>
      </div>}

      {error && <div className="inline-error" role="alert"><strong>{submitting ? 'Starting research…' : 'Could not start research'}</strong><span>{error}</span></div>}
      <div className="wizard-actions">{step > 0 && <button type="button" className="button button-quiet" onClick={() => { setError(''); setStep((current) => current - 1) }}><ArrowLeft size={16} /> Back</button>}<span className="wizard-spacer" />{step < 2 ? <button type="button" className="button button-primary" onClick={continueStep}>Continue <ArrowRight size={16} /></button> : <button type="submit" className="button button-primary" disabled={submitting || !outputType}>{submitting ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={16} />}{submitting ? 'Creating task…' : 'Start research'}</button>}</div>
      <p className="privacy-note">Queries are sent to Tavily. Candidate pages and content are processed by Firecrawl. Uploaded text is stored with your task in Supabase.</p>
    </form>
  </section>
}
