import { useCallback, useEffect, useState } from 'react'
import { getResearch, getResearchFindings, getResearchSources, getResearchSteps, openResearchEvents } from '../api/client'

const eventTypes = [
  'task_status', 'step_started', 'tool_call_started', 'mcp_tool_invocation', 'source_discovered',
  'finding_extracted', 'step_completed', 'task_completed', 'task_error',
  'retry_started', 'tool_call_finished', 'verification_result', 'step_failed',
  'recovery_started', 'task_resumed', 'task_cancel_requested', 'worker_interrupted',
  'finding_rejected', 'artifact_generated',
]

export function useResearchEvents(taskId) {
  const [events, setEvents] = useState([])
  const [task, setTask] = useState(null)
  const [sources, setSources] = useState([])
  const [findings, setFindings] = useState([])
  const [steps, setSteps] = useState([])
  const [connectionError, setConnectionError] = useState('')
  const [liveStatus, setLiveStatus] = useState('QUEUED')
  const [reconnecting, setReconnecting] = useState(false)
  const [reconnectVersion, setReconnectVersion] = useState(0)

  const reconnect = useCallback(() => setReconnectVersion((version) => version + 1), [])

  const refreshSaved = useCallback(async () => {
    if (!taskId) return
    const [taskData, sourceData, findingData, stepData] = await Promise.all([
      getResearch(taskId), getResearchSources(taskId), getResearchFindings(taskId),
      getResearchSteps(taskId),
    ])
    setTask(taskData)
    setLiveStatus(taskData.status)
    setSources(sourceData.sources || [])
    setFindings(findingData.findings || [])
    setSteps(stepData.steps || [])
    return taskData
  }, [taskId])

  useEffect(() => {
    if (!taskId) return undefined
    let source
    let closed = false
    let refreshTimer
    setEvents([])
    setTask(null)
    setLiveStatus('QUEUED')
    setSources([])
    setFindings([])
    setSteps([])
    setConnectionError('')
    refreshSaved().catch((error) => { if (!closed) setConnectionError(error.message) })
    try {
      // Start without a query cursor so a full page reload can replay the complete
      // persisted history. EventSource sends Last-Event-ID itself on reconnect.
      source = openResearchEvents(taskId)
      source.onopen = () => { if (!closed) { setReconnecting(false); setConnectionError('') } }
      eventTypes.forEach((type) => source.addEventListener(type, async (event) => {
        const data = JSON.parse(event.data)
        // Tool and step events also carry a `status` field (for example,
        // tool_call_finished: SUCCEEDED). Those statuses must not overwrite
        // the persisted research task status shown in the header.
        if (data.status && ['task_status', 'task_completed', 'task_error', 'task_resumed', 'recovery_started', 'worker_interrupted'].includes(type)) {
          setLiveStatus(data.status)
          setTask((current) => current ? { ...current, ...data, status: data.status } : current)
        }
        const id = event.lastEventId || `${Date.now()}`
        setEvents((current) => current.some((item) => item.id === id && item.type === type) ? current : [...current, { id, type, data }])
        if (['task_completed', 'task_error'].includes(type) || (type === 'task_status' && ['COMPLETED', 'COMPLETED_WITH_LIMITATIONS', 'FAILED', 'CANCELLED'].includes(data.status))) {
          clearTimeout(refreshTimer)
          source.close()
          try { if (!closed) await refreshSaved() }
          catch (error) { if (!closed) setConnectionError(error.message) }
        } else if (type === 'worker_interrupted') {
          try { if (!closed) await refreshSaved() }
          catch (error) { if (!closed) setConnectionError(error.message) }
        } else if (['source_discovered', 'step_started', 'step_completed', 'step_failed', 'finding_extracted', 'finding_rejected', 'verification_result'].includes(type)) {
          // Keep counters and persisted step statuses in sync with activity,
          // while coalescing bursts such as a batch of discovered sources.
          clearTimeout(refreshTimer)
          refreshTimer = setTimeout(() => {
            if (!closed) refreshSaved().catch((error) => setConnectionError(error.message))
          }, 250)
        }
      }))
      source.onerror = () => {
        if (!closed) { setReconnecting(true); setConnectionError('Live progress connection interrupted. Reconnecting and replaying saved task events…') }
      }
    } catch (error) { setConnectionError(error.message) }
    return () => { closed = true; clearTimeout(refreshTimer); source?.close() }
  }, [taskId, refreshSaved, reconnectVersion])

  return { events, task, sources, findings, steps, connectionError, liveStatus, reconnecting, refreshSaved, reconnect }
}
