const API_BASE = '/api'

export interface CallRecord {
  call_id: string
  customer_phone: string
  customer_name: string
  patient_name?: string
  hospital_name?: string
  claim_id?: string
  claim_type?: string
  tpa_name?: string
  payer_name?: string
  npi?: string
  policy_number?: string
  claim_age_days?: number | null
  scenario_id?: string
  tpa_executive_profile?: string
  tpa_sector_id?: string
  focus_topics?: string[]
  status: string
  lead_score?: number  // legacy alias; use confidence_score
  confidence_score?: number
  resolution_status?: string
  resolution_code?: string
  denial_code?: string
  pending_documents?: string[]
  expected_payment_date?: string
  follow_up_date?: string
  extracted_data?: Record<string, unknown>
  latency_summary?: {
    turns_with_metrics?: number
    p50_handler_ms?: number
    p95_handler_ms?: number
    p50_gap_since_agent_ms?: number | null
    p50_input_wait_ms?: number | null
    p50_agent_audio_ms?: number | null
    p50_llm_ms?: number | null
    p50_tts_ms?: number | null
    p50_prompt_ms?: number | null
    p50_eos_to_first_audio_ms?: number | null
    p95_eos_to_first_audio_ms?: number | null
    p50_first_llm_token_ms?: number | null
    p50_first_tts_chunk_ms?: number | null
    p95_stt_ms?: number | null
    p95_llm_ms?: number | null
    p95_tts_ms?: number | null
    p95_first_llm_token_ms?: number | null
    p95_first_tts_chunk_ms?: number | null
    p95_twilio_delay_ms?: number | null
    latency_slo?: {
      metric?: string
      definition?: string
      target_ms?: number
      p50_ms?: number | null
      p95_ms?: number | null
      sample_count?: number
      met?: boolean | null
    }
    llm_turns?: number
    tts_cache_hit_rate?: number | null
    p50_webhook_to_audio_fetch_ms?: number | null
    p50_llm_request_ms?: number | null
    p50_audio_fetch_delay_ms?: number | null
    p50_caller_start_ms?: number | null
    p50_caller_speaking_ms?: number | null
    p50_speech_end_to_webhook_ms?: number | null
    p95_webhook_to_audio_fetch_ms?: number | null
    slm_turns?: number
    tts_fallback_turns?: number
    gather_deferred_turns?: number
  }
  transcript: {
    role: string
    text: string
    language?: string
    emotion?: string
    stage?: string
    verbal?: Record<string, unknown>
    metrics?: {
      turn_id?: number
      path?: string
      handler_ms?: number
      llm_ms?: number
      tts_ms?: number
      tts_cache_hit?: boolean
      prompt_build_ms?: number
      context_ms?: number
      context_label?: string
      gap_since_last_agent_ms?: number | null
      previous_agent_audio_ms?: number | null
      input_wait_ms?: number | null
      twilio_stt_confidence?: number | null
      backchannel_to_main_ms?: number
      endpointing_ms?: number | null
      speech_end_to_stt_final_ms?: number | null
      first_llm_token_ms?: number | null
      first_tts_chunk_ms?: number | null
      first_playback_ms?: number | null
      eos_to_first_audio_ms?: number | null
      orchestration_ms?: number | null
      webhook_to_audio_fetch_ms?: number | null
      twilio_fetch_wait_ms?: number | null
      llm_request_ms?: number | null
      llm_output_tokens?: number | null
      llm_thought_tokens?: number | null
      llm_attempts?: number
      llm_error?: string | null
      audio_fetch_delay_ms?: number | null
      caller_start_ms?: number | null
      caller_speaking_ms?: number | null
      speech_end_to_webhook_ms?: number | null
      speech_end_measured?: boolean
      gather_deferred?: boolean
      llm_model?: string | null
      model_tier?: string | null
      slm_used?: boolean
      model_fallback_used?: boolean
      tts_provider?: string | null
      tts_voice?: string | null
      tts_fallback_used?: boolean
    }
  }[]
  /** Live STT caption while the other party is speaking (before committed turn) */
  live_transcript?: {
    text?: string
    is_final?: boolean
    updated_at?: number
    model?: string
    source?: string
    missed?: boolean
    committed?: boolean
    error?: string
  }
  duration_seconds: number
  created_at: number
}

export interface HealthStatus {
  status: string
  twilio_configured: boolean
  vapi_configured?: boolean
  call_provider?: string
  dialogue_mode?: string
  stt_configured?: boolean
  tts_configured?: boolean
  tts_provider?: string
  elevenlabs_configured?: boolean
  gemini_configured: boolean
  public_url: string
  public_url_ready: boolean
  public_url_source?: string
  webhook_reachable?: boolean
  webhook_message?: string
}

export const CLAIM_TYPES = [
  'Claim Pending',
  'Claim Denied',
  'Unsettled Claim',
  'Cashless Authorization Pending',
  'Claim Rejected',
  'Additional Documents Required',
  'Medical Coding Clarification',
  'Authorization Pending',
  'Reimbursement Status',
  'Reimbursement Follow-up',
  'Payment Delay',
  'Escalation Request',
  'Claim Approved',
  'Coverage Limit',
  'Missing Documents',
  'Appeal Process',
]

export const TPA_PROFILES = [
  'Helpful Executive',
  'Busy Executive',
  'Experienced Executive',
  'New Executive',
  'Cooperative Executive',
  'Uncooperative Executive',
  'Process Driven Executive',
  'Rule Focused Executive',
  'Empathetic Executive',
  'Impatient Executive',
  'Escalation Focused Executive',
  'Supervisor Type',
  'Technical Executive',
  'Incomplete Information Executive',
  'Slow Response Executive',
  'Fast Response Executive',
  'Knowledgeable Executive',
  'Confused Executive',
  'Defensive Executive',
  'Polite Executive',
]

export async function fetchHealth(): Promise<HealthStatus> {
  const res = await fetch(`${API_BASE}/health`, { signal: AbortSignal.timeout(8000) })
  if (!res.ok) throw new Error('Call service is not reachable yet')
  return res.json()
}

export async function savePublicUrl(public_url: string): Promise<{
  public_url: string
  public_url_ready: boolean
  message: string
}> {
  const res = await fetch(`${API_BASE}/public-url`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ public_url }),
  })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.detail || 'Failed to save PUBLIC_URL')
  }
  return res.json()
}

export async function connectPublicUrl(): Promise<{
  public_url: string
  public_url_ready: boolean
  message: string
}> {
  const res = await fetch(`${API_BASE}/public-url/connect`, {
    method: 'POST',
    signal: AbortSignal.timeout(12000),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => null)
    throw new Error(err?.detail || 'Could not connect the public link')
  }
  return res.json()
}

export async function discoverPublicUrl(): Promise<{
  public_url: string
  public_url_ready: boolean
  message: string
}> {
  const res = await fetch(`${API_BASE}/public-url/discover`, { method: 'POST' })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.detail || 'Could not detect ngrok')
  }
  return res.json()
}

export async function downloadCallJson(callId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/calls/${callId}/download`)
  if (!res.ok) throw new Error('Failed to download call data')
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `call_${callId}.json`
  a.click()
  URL.revokeObjectURL(url)
}

export async function fetchCalls(): Promise<CallRecord[]> {
  const res = await fetch(`${API_BASE}/calls`)
  const data = await res.json()
  return data.calls
}

export async function fetchCall(callId: string): Promise<CallRecord> {
  const res = await fetch(`${API_BASE}/calls/${callId}`)
  if (!res.ok) throw new Error('Call not found')
  return res.json()
}

export async function initiateCall(params: {
  phone_number: string
  customer_name?: string
  patient_name?: string
  agent_name?: string
  preferred_language?: string
  hospital_name?: string
  claim_id?: string
  claim_type?: string
  tpa_name?: string
  payer_name?: string
  npi?: string
  policy_number?: string
  claim_age_days?: number | null
  scenario_id?: string
  tpa_executive_profile?: string
  tpa_sector_id?: string
  focus_topics?: string[]
}): Promise<{ call_id: string; call_sid: string; status: string; message: string }> {
  const res = await fetch(`${API_BASE}/calls/initiate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => null)
    const detail = err?.detail
    const message = Array.isArray(detail)
      ? detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join(', ')
      : typeof detail === 'string'
        ? detail
        : err?.message || `Failed to initiate call (HTTP ${res.status})`
    throw new Error(message)
  }
  return res.json()
}

export async function fetchLiveGreeting(callId: string): Promise<{ text: string; audio_base64: string }> {
  const res = await fetch(`${API_BASE}/calls/${callId}/live-greeting`, { method: 'POST' })
  if (!res.ok) throw new Error('Could not play the opening line')
  return res.json()
}

export async function sendLiveAudio(
  callId: string,
  audio: Blob,
): Promise<{ text: string; audio_base64: string; heard?: string }> {
  const body = new FormData()
  body.append('file', audio, 'speech.webm')
  const res = await fetch(`${API_BASE}/calls/${callId}/live-hear`, { method: 'POST', body })
  if (!res.ok) throw new Error('Could not hear that')
  return res.json()
}

export interface TpaSector {
  id: string
  number: number
  name: string
  short_name: string
  description: string
  topics: string[]
  sample_questions: string[]
}

export async function fetchTpaSectors(): Promise<{
  count: number
  sectors: TpaSector[]
  message?: string
}> {
  const res = await fetch(`${API_BASE}/calls/tpa-sectors`)
  if (!res.ok) throw new Error('Failed to load TPA sectors')
  return res.json()
}

export const DEFAULT_LANGUAGE = 'en-US'

export interface ScenarioRecord {
  scenario_id?: string
  id?: string
  claim_type?: string
  claim_scenario_type?: string
  tpa_executive_profile?: string
  conversation_context?: string
  ai_response?: string
  executive_response?: string
  call_outcome?: string
  expected_outcome?: string
  emotion?: string
  negotiation_technique?: string
  soft_skill_applied?: string
  [key: string]: unknown
}

export async function fetchScenarios(profile?: string): Promise<{
  count: number
  profiles: string[]
  scenarios: ScenarioRecord[]
}> {
  const url = profile
    ? `${API_BASE}/calls/scenarios?profile=${encodeURIComponent(profile)}`
    : `${API_BASE}/calls/scenarios`
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed to fetch scenarios')
  return res.json()
}

export async function fetchScenarioDuplicates(threshold = 0.82): Promise<{
  total_scenarios: number
  exact_duplicate_ids: string[]
  near_duplicate_pairs: Array<{
    scenario_a: string
    scenario_b: string
    similarity: number
    same_profile: boolean
    same_claim_type: boolean
    profile_a?: string
    profile_b?: string
    claim_type_a?: string
    claim_type_b?: string
  }>
  near_duplicate_count: number
  flagged_count: number
  duplicate_percentage: number
  similarity_threshold: number
  status: 'pass' | 'review'
  message: string
}> {
  const res = await fetch(`${API_BASE}/calls/scenarios/duplicates?threshold=${threshold}`)
  if (!res.ok) throw new Error('Failed to audit scenario duplicates')
  return res.json()
}

export async function fetchScenarioById(scenarioId: string): Promise<ScenarioRecord> {
  const res = await fetch(`${API_BASE}/calls/scenarios/${encodeURIComponent(scenarioId)}`)
  if (!res.ok) throw new Error('Scenario not found')
  const data = await res.json()
  return data.scenario
}

export interface ConversationQualityPayload {
  voice: {
    stability: number
    similarity: number
    style: number
    speed: number
    volume_gain: number
    speaker_boost: boolean
    voice_id_configured: boolean
    model_id: string
    emotion_note: string
    backchannel_enabled: boolean
    low_latency_calls: boolean
    twilio_gather_speech_timeout: string
  }
  verbal_habits: {
    enabled: boolean
    reply_variants_per_step: number
    description: string
    ack_tiers: { id: string; label: string; examples: string[] }[]
    backchannel_phrases: string[]
    gemini_polish: string
  }
  acknowledgement_policy: {
    avoid_repeated_formal: boolean
    formal_ack_list: string[]
    session_memory: string
    after_backchannel_rule: string
  }
  turn_latency: {
    enabled: boolean
    per_turn_fields: string[]
    call_level_summary: string
    view_in_ui: string
  }
}

export async function fetchConversationQuality(): Promise<ConversationQualityPayload> {
  const res = await fetch(`${API_BASE}/conversation-quality`)
  if (!res.ok) throw new Error('Failed to load conversation quality settings')
  return res.json()
}

export async function patchVoiceSettings(
  patch: Partial<{
    stability: number
    similarity: number
    style: number
    speed: number
    volume_gain: number
    speaker_boost: boolean
    backchannel_enabled: boolean
    low_latency_calls: boolean
    twilio_gather_speech_timeout: string
  }>
): Promise<{ updated: string[]; voice: ConversationQualityPayload['voice'] }> {
  const res = await fetch(`${API_BASE}/conversation-quality/voice`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || 'Failed to save voice settings')
  }
  return res.json()
}

export async function previewVoiceSample(): Promise<Blob> {
  const res = await fetch(`${API_BASE}/conversation-quality/preview`, { method: 'POST' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(typeof err.detail === 'string' ? err.detail : 'Preview failed')
  }
  return res.blob()
}
