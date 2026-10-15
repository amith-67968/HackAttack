import React from 'react'

const labels = {
  QUEUED: 'Queued', PLANNING: 'Planning', RUNNING: 'Researching', VERIFYING: 'Verifying',
  RECOVERING: 'Recovering', GENERATING_OUTPUT: 'Creating deliverables', AWAITING_APPROVAL: 'Awaiting review',
  COMPLETED: 'Completed', COMPLETED_WITH_LIMITATIONS: 'Completed with limitations',
  FAILED: 'Failed', CANCELLED: 'Cancelled', PENDING: 'Pending', RETRYING: 'Retrying',
  SUCCEEDED: 'Succeeded', VERIFIED: 'Verified', SKIPPED: 'Skipped',
}

export default function StatusBadge({ status }) {
  const normalized = String(status || 'UNKNOWN').toUpperCase()
  const kind = ['COMPLETED', 'VERIFIED', 'SUCCEEDED'].includes(normalized) ? 'success'
    : ['FAILED', 'CANCELLED'].includes(normalized) ? 'danger'
      : ['COMPLETED_WITH_LIMITATIONS', 'RECOVERING', 'RETRYING'].includes(normalized) ? 'warning' : 'neutral'
  return <span className={`status-badge status-${kind}`} data-status={normalized}>{labels[normalized] || normalized.replaceAll('_', ' ')}</span>
}
