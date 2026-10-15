const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'

export async function getHealth() {
  const response = await fetch(`${API_BASE_URL}/api/health`)
  if (!response.ok) throw new Error(`Health request failed (${response.status})`)
  return response.json()
}

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, options)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload.detail
    const message = typeof detail === 'string' ? detail : detail?.message || `Request failed (${response.status})`
    const missing = detail?.missing?.length ? ` Missing configuration: ${detail.missing.join(', ')}.` : ''
    throw new Error(`${message}${missing}`)
  }
  return payload
}

export function createResearch(input) {
  return request('/api/research', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) })
}

export function getResearch(taskId) { return request(`/api/research/${taskId}`) }
export function getResearchSources(taskId) { return request(`/api/research/${taskId}/sources`) }
export function getResearchFindings(taskId) { return request(`/api/research/${taskId}/findings`) }
export function getResearchArtifacts(taskId) { return request(`/api/research/${taskId}/artifacts`) }
export function createResearchArtifact(taskId, input) {
  return request(`/api/research/${taskId}/artifacts`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) })
}
export function artifactDownloadUrl(artifactId) { return `${API_BASE_URL}/api/artifacts/${artifactId}/download` }

export function resumeResearch(taskId) {
  return request(`/api/research/${taskId}/resume`, { method: 'POST' })
}

export function cancelResearch(taskId) {
  return request(`/api/research/${taskId}/cancel`, { method: 'POST' })
}

export function openResearchEvents(taskId, after = 0) {
  const suffix = after ? `?after=${encodeURIComponent(after)}` : ''
  return new EventSource(`${API_BASE_URL}/api/research/${taskId}/events${suffix}`)
}
