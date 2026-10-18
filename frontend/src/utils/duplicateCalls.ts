import type { CallRecord } from '../api'

const ACTIVE_STATUSES = new Set(['initiated', 'calling', 'ringing', 'answered', 'in-progress'])
const RECENT_HOURS = 24

export interface LiveDuplicateRisk {
  claim_id: string
  phone: string
  reason: 'active_call' | 'recent_completed'
  message: string
  call_id: string
  status: string
  patient_name?: string
}

function claimPhoneKey(claimId: string, phone: string): string | null {
  const claim = (claimId || '').replace(/\s/g, '').toLowerCase()
  const digits = (phone || '').replace(/\D/g, '')
  if (!claim || !digits) return null
  return `${claim}|${digits}`
}

/** Mirrors backend check_call_duplicate — surfaces claims that would be blocked on redial. */
export function findLiveDuplicateRisks(calls: CallRecord[]): LiveDuplicateRisk[] {
  const cutoff = Date.now() / 1000 - RECENT_HOURS * 3600
  const risks: LiveDuplicateRisk[] = []
  const seen = new Set<string>()

  for (const record of calls) {
    const key = claimPhoneKey(record.claim_id || '', record.customer_phone)
    if (!key || seen.has(key)) continue

    const status = record.status
    let reason: LiveDuplicateRisk['reason'] | null = null

    if (ACTIVE_STATUSES.has(status)) {
      reason = 'active_call'
    } else if (status === 'completed' && record.created_at >= cutoff) {
      reason = 'recent_completed'
    }

    if (!reason) continue

    seen.add(key)
    risks.push({
      claim_id: record.claim_id || key.split('|')[0],
      phone: record.customer_phone,
      reason,
      message:
        reason === 'active_call'
          ? 'A follow-up for this claim is already in progress. Redial is blocked.'
          : `This claim was followed up within the last ${RECENT_HOURS} hours. Redial is blocked.`,
      call_id: record.call_id,
      status,
      patient_name: record.patient_name || record.customer_name,
    })
  }

  return risks.sort((a, b) => {
    if (a.reason === b.reason) return 0
    return a.reason === 'active_call' ? -1 : 1
  })
}
