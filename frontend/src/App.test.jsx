import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'

afterEach(() => { cleanup(); window.localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('ProofLoop status page', () => {
  it('shows truthful component state from the health endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({
      components: {
        api: { status: 'available' },
        ollama: { status: 'model_missing', connected: true, model: 'qwen3:4b', model_available: false, available_models: [] },
        supabase: { status: 'not_configured', connected: false },
      },
    }) }))
    render(<App />)
    expect(await screen.findByText('Model missing')).toBeInTheDocument()
    expect(screen.getByText('Not configured')).toBeInTheDocument()
    expect(screen.getByText('qwen3:4b')).toBeInTheDocument()
  })

  it('submits a research request and explains missing provider configuration', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ components: {} }) })
      .mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({ detail: { message: 'Configure backend providers.', missing: ['TAVILY_API_KEY'] } }) }))
    render(<App />)
    fireEvent.change(screen.getByLabelText('What would you like to research?'), { target: { value: 'Find clinic consultation prices' } })
    fireEvent.click(screen.getByRole('button', { name: /start research/i }))
    expect(await screen.findByText(/TAVILY_API_KEY/)).toBeInTheDocument()
  })

  it('renders saved source-backed findings after genuine SSE lifecycle events', async () => {
    const taskId = '8c6c0bfa-0474-4ea1-b45d-84f7bcfd12a1'
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ components: {} }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ task_id: taskId, status: 'QUEUED', events_url: `/api/research/${taskId}/events` }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: taskId, status: 'RUNNING' }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ sources: [] }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ findings: [] }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ artifacts: [] }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: taskId, status: 'COMPLETED', summary: 'Read one source.' }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ sources: [{ id: 'source-1', url: 'https://example.org/fees', title: 'Clinic fees', domain: 'example.org', fetch_status: 'retrieved', retrieved_at: '2026-10-15T00:00:00Z' }] }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ findings: [{ id: 'finding-1', source_id: 'source-1', entity_name: 'Alpha Clinic', field_name: 'price', field_value: '$50', value_status: 'found', verification_status: 'source_supported', supporting_excerpt: 'Consultation: $50.', confidence_notes: 'Not cross-checked.' }] }) }))

    class FakeEventSource {
      static latest
      constructor() { this.listeners = {}; FakeEventSource.latest = this }
      addEventListener(type, callback) { this.listeners[type] = callback }
      close() {}
      emit(type, data) { this.listeners[type]?.({ data: JSON.stringify(data), lastEventId: String(Date.now()) }) }
    }
    vi.stubGlobal('EventSource', FakeEventSource)

    render(<App />)
    fireEvent.change(screen.getByLabelText('What would you like to research?'), { target: { value: 'Find clinic consultation prices' } })
    fireEvent.click(screen.getByRole('button', { name: /start research/i }))
    expect(await screen.findByText('QUEUED')).toBeInTheDocument()
    await waitFor(() => expect(FakeEventSource.latest).toBeDefined())
    FakeEventSource.latest.emit('task_status', { task_id: taskId, status: 'RUNNING', message: 'Creating a plan' })
    expect(await screen.findByText('RUNNING')).toBeInTheDocument()
    FakeEventSource.latest.emit('task_completed', { task_id: taskId, status: 'COMPLETED', summary: 'Read one source.' })
    expect(await screen.findByText('Alpha Clinic')).toBeInTheDocument()
    expect(screen.getByText('Clinic fees')).toHaveAttribute('href', 'https://example.org/fees')
    expect(screen.getByText('source supported')).toBeInTheDocument()
  })

  it('generates and edits an email draft without sending it', async () => {
    const taskId = '6f398113-ded4-4ff0-9b37-7ab9ad01cc89'
    window.localStorage.setItem('proofloop-current-task', taskId)
    const draft = 'To: (add recipient)\nSubject: Saved findings\n\nThis is a draft.'
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ components: {} }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: taskId, status: 'COMPLETED' }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ sources: [] }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ findings: [] }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ artifacts: [] }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: 'artifact-1', task_id: taskId, artifact_type: 'email', filename: 'draft.txt', status: 'READY', preview: { body: draft, draft_only: true } }) }))
    render(<App />)
    fireEvent.change(await screen.findByLabelText('Artifact format'), { target: { value: 'email' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate deliverable' }))
    const editor = await screen.findByLabelText('Email draft editor')
    expect(editor).toHaveValue(draft)
    fireEvent.change(editor, { target: { value: `${draft}\nEdited` } })
    expect(editor).toHaveValue(`${draft}\nEdited`)
    expect(screen.getByText('This is a draft. ProofLoop does not send email.')).toBeInTheDocument()
  })
})
