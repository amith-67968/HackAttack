import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'

const taskId = '8c6c0bfa-0474-4ea1-b45d-84f7bcfd12a1'
const health = { components: {
  api: { status: 'available' },
  ollama: { status: 'connected', connected: true, model: 'qwen3:4b', model_available: true, available_models: ['qwen3:4b'] },
  supabase: { status: 'configured', connected: false },
  tavily: { status: 'configured' }, firecrawl: { status: 'configured' },
} }
const completedTask = { id: taskId, query: 'Compare two local clinics', status: 'COMPLETED', summary: 'Read one source and saved one supported finding.', created_at: '2026-10-15T00:00:00Z', started_at: '2026-10-15T00:00:00Z', completed_at: '2026-10-15T00:05:00Z', plan: { topic: 'Clinics', summary: 'Compare official clinic pages.', required_fields: ['consultation fee'], actions: [] } }
const savedSource = { id: 'source-1', task_id: taskId, url: 'https://example.org/fees', canonical_url: 'https://example.org/fees', title: 'Clinic fees', domain: 'example.org', source_type: 'web_search', fetch_status: 'retrieved', retrieved_at: '2026-10-15T00:02:00Z', content_excerpt: 'Alpha Clinic consultation fee is $50.' }
const savedFinding = { id: 'finding-1', source_id: 'source-1', entity_name: 'Alpha Clinic', field_name: 'consultation fee', field_value: '$50', value_status: 'found', verification_status: 'source_supported', supporting_excerpt: 'Alpha Clinic consultation fee is $50.' }

class FakeEventSource {
  static latest
  constructor(url) { this.url = url; this.listeners = {}; this.closed = false; FakeEventSource.latest = this }
  addEventListener(type, callback) { this.listeners[type] = callback }
  close() { this.closed = true }
  emit(type, data, id = '1') { this.listeners[type]?.({ data: JSON.stringify(data), lastEventId: id }) }
  open() { this.onopen?.() }
}

function mockApi({ tasks = [], sources = [savedSource], findings = [savedFinding], steps = [], artifacts = [], task = completedTask, createResponse, createArtifactResponse } = {}) {
  const fetchMock = vi.fn(async (input, options = {}) => {
    const url = new URL(String(input), 'http://localhost')
    const method = options.method || 'GET'
    if (url.pathname === '/api/health') return response(health)
    if (url.pathname === '/api/research' && method === 'GET') return response({ tasks })
    if (url.pathname === '/api/research' && method === 'POST') return response(createResponse || { task_id: taskId, status: 'QUEUED', events_url: `/api/research/${taskId}/events` }, 202)
    if (url.pathname === '/api/sources') return response({ sources })
    if (url.pathname === `/api/research/${taskId}`) return response(task)
    if (url.pathname === `/api/research/${taskId}/sources`) return response({ sources })
    if (url.pathname === `/api/research/${taskId}/findings`) return response({ findings })
    if (url.pathname === `/api/research/${taskId}/steps`) return response({ steps })
    if (url.pathname === `/api/research/${taskId}/artifacts` && method === 'GET') return response({ artifacts })
    if (url.pathname === `/api/research/${taskId}/artifacts` && method === 'POST') return response(createArtifactResponse || { id: 'artifact-1', task_id: taskId, artifact_type: JSON.parse(options.body).artifact_type, filename: 'research.md', status: 'READY', preview: { markdown: '# Saved research', task_status: task.status } }, 201)
    return response({ detail: `Unexpected request ${method} ${url.pathname}` }, 404)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function response(payload, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => payload }
}

function goto(path) { window.history.pushState({}, '', path) }

beforeEach(() => {
  window.history.pushState({}, '', '/')
  window.localStorage.clear()
  FakeEventSource.latest = null
  vi.stubGlobal('EventSource', FakeEventSource)
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('ProofLoop research journey', () => {
  it('renders a real-health landing page and navigates from its primary CTA', async () => {
    mockApi({ tasks: [{ ...completedTask, id: 'recent-1', query: 'Recent task' }] })
    render(<App />)
    expect(await screen.findByRole('heading', { name: /research that shows its work/i })).toBeInTheDocument()
    expect(await screen.findByText('Recent task')).toBeInTheDocument()
    expect(screen.getByText('Ollama')).toBeInTheDocument()
    expect(screen.queryByLabelText('Research question')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /start researching/i }))
    expect(await screen.findByRole('heading', { name: /give your question a clear starting point/i })).toBeInTheDocument()
  })

  it('validates a research question before leaving the first setup step', async () => {
    mockApi()
    goto('/research/new')
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: /continue/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Add a research question')
    expect(screen.getByLabelText('Research question')).toBeInTheDocument()
  })

  it('accepts small TXT/Markdown files, rejects PDF, validates URLs, and creates a task using the API response ID', async () => {
    const fetchMock = mockApi({ createResponse: { task_id: taskId, status: 'QUEUED', events_url: `/api/research/${taskId}/events` }, task: { ...completedTask, status: 'RUNNING' } })
    goto('/research/new')
    render(<App />)
    fireEvent.change(screen.getByLabelText('Research question'), { target: { value: 'Compare the supplied clinic details' } })
    fireEvent.click(screen.getByRole('button', { name: /continue/i }))

    const picker = document.querySelector('input[type="file"]')
    const note = new File(['Alpha Clinic fee is $50.'], 'clinic-notes.md', { type: 'text/markdown' })
    const unsupported = new File(['not parsed'], 'scan.pdf', { type: 'application/pdf' })
    Object.defineProperty(note, 'arrayBuffer', { value: async () => new TextEncoder().encode('Alpha Clinic fee is $50.').buffer })
    fireEvent.change(picker, { target: { files: [note, unsupported] } })
    expect(await screen.findByText('clinic-notes.md')).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent('only TXT and Markdown')

    fireEvent.change(screen.getByLabelText(/Reference webpages/), { target: { value: 'not a URL' } })
    fireEvent.click(screen.getByRole('button', { name: /add url/i }))
    expect(screen.getByText('Enter a valid public HTTP or HTTPS webpage URL.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(/Reference webpages/), { target: { value: 'https://example.org/fees' } })
    fireEvent.click(screen.getByRole('button', { name: /add url/i }))
    expect(await screen.findByText('https://example.org/fees')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /continue/i }))
    fireEvent.click(screen.getByRole('button', { name: /start research/i }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/research'), expect.objectContaining({ method: 'POST' })))
    const body = JSON.parse(fetchMock.mock.calls.find(([, options]) => options?.method === 'POST')[1].body)
    expect(body.input_materials).toEqual([{ filename: 'clinic-notes.md', content: 'Alpha Clinic fee is $50.' }])
    expect(body.options.reference_urls).toEqual(['https://example.org/fees'])
    expect(await screen.findByText('Research is in progress.')).toBeInTheDocument()
    expect(window.location.pathname).toBe(`/research/${taskId}/progress`)
    expect(FakeEventSource.latest.url).toContain(`/api/research/${taskId}/events`)
  })

  it('restores task progress, shows saved steps, and reconnects when the SSE connection returns', async () => {
    mockApi({ task: { ...completedTask, status: 'COMPLETED_WITH_LIMITATIONS', resume_eligible: false, cancel_eligible: false }, steps: [{ id: 'step-1', action_key: 'search_one', title: 'Search official pages', status: 'VERIFIED' }] })
    goto(`/research/${taskId}/progress`)
    render(<App />)
    expect(await screen.findByText('Search official pages')).toBeInTheDocument()
    expect(FakeEventSource.latest).toBeDefined()
    FakeEventSource.latest.onerror()
    expect(await screen.findByText('Reconnecting')).toBeInTheDocument()
    FakeEventSource.latest.open()
    expect(await screen.findByText('Saved history')).toBeInTheDocument()
    FakeEventSource.latest.emit('step_started', { title: 'A replayed saved event.' }, '9')
    expect(await screen.findByText('A replayed saved event.')).toBeInTheDocument()
    expect(await screen.findByText('Completed with limitations.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /open results/i }))
    expect(window.location.pathname).toBe(`/research/${taskId}/results`)
  })

  it('keeps task status separate from tool status and refreshes persisted counters during live work', async () => {
    const runningTask = { ...completedTask, status: 'RUNNING', resume_eligible: false, cancel_eligible: true }
    let sources = []
    let findings = []
    let steps = [{ id: 'plan', action_key: 'create_plan', title: 'Create a plan', status: 'VERIFIED' }]
    vi.stubGlobal('fetch', vi.fn(async (input) => {
      const url = new URL(String(input), 'http://localhost')
      if (url.pathname === `/api/research/${taskId}`) return response(runningTask)
      if (url.pathname === `/api/research/${taskId}/sources`) return response({ sources })
      if (url.pathname === `/api/research/${taskId}/findings`) return response({ findings })
      if (url.pathname === `/api/research/${taskId}/steps`) return response({ steps })
      if (url.pathname === '/api/health') return response(health)
      if (url.pathname === '/api/sources') return response({ sources: [] })
      return response({ tasks: [] })
    }))
    goto(`/research/${taskId}/progress`)
    render(<App />)
    expect(await screen.findByText('Researching')).toBeInTheDocument()
    expect(screen.getAllByText('0', { selector: '.progress-stats strong' })).toHaveLength(2)
    expect(screen.getAllByText('1', { selector: '.progress-stats strong' })).toHaveLength(1)

    FakeEventSource.latest.emit('tool_call_finished', { status: 'SUCCEEDED', tool: 'ollama_extract' }, '2')
    expect(screen.getByText('Researching')).toBeInTheDocument()
    expect(screen.queryByText('Succeeded')).not.toBeInTheDocument()

    sources = [savedSource]
    findings = [savedFinding]
    steps = [...steps, { id: 'read-1', action_key: 'read_one', title: 'Read a source', status: 'VERIFIED' }]
    FakeEventSource.latest.emit('step_completed', { status: 'VERIFIED', step_id: 'read_one' }, '3')
    await waitFor(() => {
      expect(screen.getAllByText('2', { selector: '.progress-stats strong' })).toHaveLength(1)
      expect(screen.getAllByText('1', { selector: '.progress-stats strong' })).toHaveLength(2)
    })
    expect(screen.getByText('Researching')).toBeInTheDocument()
  })

  it('renders source-backed findings, limitations, and can generate a saved report', async () => {
    const failedTask = { ...completedTask, status: 'COMPLETED_WITH_LIMITATIONS', error_summary: 'Some requested fields were not found.' }
    const fetchMock = mockApi({ task: failedTask, sources: [savedSource], findings: [savedFinding], artifacts: [] })
    goto(`/research/${taskId}/results`)
    render(<App />)
    expect(await screen.findByText('Completed with limitations')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /findings/i }))
    expect(await screen.findByText('Alpha Clinic')).toBeInTheDocument()
    expect(await screen.findByText('$50')).toBeInTheDocument()
    expect(screen.getByText('Alpha Clinic consultation fee is $50.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /deliverables/i }))
    fireEvent.click(screen.getByRole('button', { name: /generate report/i }))
    expect(await screen.findByText('Research report created from saved evidence.')).toBeInTheDocument()
    expect(await screen.findByText('# Saved research')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([url, options]) => String(url).includes('/artifacts') && options?.method === 'POST')).toBe(true)
  })

  it('shows an actual empty history and sources state without fake records', async () => {
    mockApi({ tasks: [], sources: [] })
    goto('/research')
    render(<App />)
    expect(await screen.findByText('No research saved yet')).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: /new research/i })[0])
    expect(await screen.findByLabelText('Research question')).toBeInTheDocument()
  })
})
