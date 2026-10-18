import type { HealthStatus } from '../api'

/** Reasons the Follow Up call button must stay disabled. Empty means it may be clicked. */
export function callLaunchBlockers(health: HealthStatus | null): string[] {
  if (!health) return ['Waiting for the call service to start.']

  const provider = (health.call_provider || 'media_streams').trim().toLowerCase()
  if (provider === 'vapi') {
    const blockers: string[] = []
    if (!health.twilio_configured) blockers.push('TWILIO_ACCOUNT_SID and TWILIO_PHONE_NUMBER')
    if (!health.vapi_configured) blockers.push('VAPI_API_KEY, VAPI_ASSISTANT_ID, and Twilio credentials')
    return blockers
  }

  const blockers: string[] = []
  if (!health.twilio_configured) {
    blockers.push('TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_PHONE_NUMBER')
  }
  if (!health.gemini_configured) blockers.push('GEMINI_API_KEY')
  if (!health.tts_configured) {
    blockers.push('A configured text-to-speech provider. Deepgram needs DEEPGRAM_API_KEY and DEEPGRAM_TTS_VOICE.')
  }
  if (!health.public_url_ready) {
    blockers.push('The public link is still connecting. It starts on its own when this page opens.')
  } else if (health.webhook_reachable === false) {
    blockers.push(health.webhook_message || 'PUBLIC_URL is not reachable.')
  }
  return blockers
}

export function isCallLaunchReady(health: HealthStatus | null): boolean {
  return health != null && callLaunchBlockers(health).length === 0
}
