import type { CallRecord } from '../api'

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'busy', 'no-answer', 'canceled'])

export function isCallTerminal(status: string | undefined): boolean {
  return !!status && TERMINAL_STATUSES.has(status)
}

export function isCallLive(status: string | undefined): boolean {
  if (!status) return false
  return !isCallTerminal(status)
}

/** Map backend call state to HorizontalClaimProgress step index (0–5). */
export function computeClaimProgressStep(
  activeCall: CallRecord | null,
  initiating: boolean,
  hasClaimContext: boolean
): number {
  if (!activeCall && !initiating) {
    return hasClaimContext ? 0 : 0
  }

  const status = activeCall?.status ?? (initiating ? 'calling' : '')
  const transcript = activeCall?.transcript ?? []

  if (status === 'completed') {
    const stages = transcript.map((t) => t.stage).filter(Boolean)
    if (stages.includes('call_summary')) return 5
    if (stages.includes('discovery') || transcript.some((t) => t.role === 'customer')) return 4
    if (stages.includes('greeting') || transcript.length > 0) return 3
    return 3
  }

  if (status === 'failed') {
    return transcript.length > 0 ? 3 : 2
  }

  if (status === 'in_progress' || status === 'answered') {
    if (transcript.some((t) => t.role === 'customer')) return 3
    if (transcript.length > 0) return 3
    return 3
  }

  if (status === 'ringing') return 2

  if (status === 'calling' || status === 'initiated') return 1

  if (initiating) return 1

  return hasClaimContext ? 0 : 0
}
