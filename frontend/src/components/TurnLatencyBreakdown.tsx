import type { CallRecord } from '../api'

type TurnRow = {
  label: string
  turnId?: number
  handler?: number
  gap?: number | null
  inputWait?: number | null
  agentAudio?: number | null
  llm?: number
  tts?: number
  prompt?: number
  sttConf?: number | null
  path?: string
  backchannelWait?: number
  verbalSource?: string
  verbalAck?: string
  eosToAudio?: number | null
  endpointing?: number | null
  sttFinal?: number | null
  firstLlmToken?: number | null
  firstTtsChunk?: number | null
  orchestration?: number | null
  audioFetch?: number | null
  fetchWait?: number | null
  deferred?: boolean
  model?: string | null
  slm?: boolean
  voice?: string | null
  ttsFallback?: boolean
}

function collectTurnRows(call: CallRecord): TurnRow[] {
  const rows: TurnRow[] = []
  for (const t of call.transcript) {
    const m = t.metrics
    if (!m || (m.handler_ms == null && m.gap_since_last_agent_ms == null)) continue
    const role = t.role === 'customer' ? 'Payer' : t.role === 'agent' ? 'Chris' : t.role
    const stage = t.stage ? ` · ${t.stage}` : ''
    rows.push({
      label: `${role}${stage}`,
      turnId: m.turn_id,
      handler: m.handler_ms,
      gap: m.gap_since_last_agent_ms,
      inputWait: m.input_wait_ms,
      agentAudio: m.previous_agent_audio_ms,
      llm: m.llm_ms,
      tts: m.tts_ms,
      prompt: m.prompt_build_ms ?? m.context_ms,
      sttConf: m.twilio_stt_confidence,
      path: m.path,
      backchannelWait: m.backchannel_to_main_ms,
      verbalSource: typeof t.verbal?.source === 'string' ? t.verbal.source : undefined,
      verbalAck: typeof t.verbal?.ack === 'string' ? t.verbal.ack : undefined,
      eosToAudio: m.eos_to_first_audio_ms,
      endpointing: m.endpointing_ms,
      sttFinal: m.speech_end_to_stt_final_ms,
      firstLlmToken: m.first_llm_token_ms,
      firstTtsChunk: m.first_tts_chunk_ms,
      orchestration: m.orchestration_ms,
      audioFetch: m.webhook_to_audio_fetch_ms,
      fetchWait: m.twilio_fetch_wait_ms,
      deferred: m.gather_deferred,
      model: m.llm_model,
      slm: m.slm_used,
      voice: m.tts_voice ?? m.tts_provider,
      ttsFallback: m.tts_fallback_used,
    })
  }
  return rows
}

function Bar({ value, max, color }: { value: number; max: number; color: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0
  return (
    <div className="h-2 flex-1 min-w-[60px] bg-slate-100 rounded-full overflow-hidden">
      <div className={`h-full ${color} rounded-full transition-all`} style={{ width: `${pct}%` }} />
    </div>
  )
}

interface Props {
  call: CallRecord
}

export default function TurnLatencyBreakdown({ call }: Props) {
  const rows = collectTurnRows(call)
  const summary = call.latency_summary
  if (rows.length === 0 && !(summary?.turns_with_metrics ?? 0)) {
    return (
      <p className="text-xs text-content-muted py-2">
        No turn timing yet. Complete a call with Gemini enabled (LOW_LATENCY_CALLS=false) to see STT, LLM, and TTS
        breakdowns.
      </p>
    )
  }

  const maxBar = Math.max(
    ...rows.flatMap((r) => [
      r.handler ?? 0,
      r.llm ?? 0,
      r.tts ?? 0,
      r.gap ?? 0,
      r.inputWait ?? 0,
      r.agentAudio ?? 0,
      r.backchannelWait ?? 0,
      r.eosToAudio ?? 0,
      r.audioFetch ?? 0,
      r.fetchWait ?? 0,
    ]),
    summary?.p95_handler_ms ?? 500,
    400
  )

  return (
    <div className="mb-4 mt-4 rounded-xl border border-slate-200 bg-white px-4 py-4">
      <h4 className="text-sm font-bold text-brand-950 mb-1">Turn latency (where lag happens)</h4>
      <p className="text-[11px] text-brand-800/80 mb-3 leading-relaxed">
        The 1200 ms target is payer speech end to first AI audio (EOS→audio). It excludes payer speaking time and input wait.
        Twilio Gather does not report speech end. Reply-to-audio-fetch includes model, speech synthesis, and the wait until Twilio fetches the file. Fetch wait is only the time after the audio is already stored.
      </p>
      {summary?.latency_slo && (
        <p className="text-[11px] text-brand-800/80 mb-3">
          Samples for {summary.latency_slo.metric || 'EOS→audio'}: {summary.latency_slo.sample_count ?? 0}.
          Target {summary.latency_slo.target_ms ?? 1200} ms is not marked passed here.
        </p>
      )}

      {summary && (summary.turns_with_metrics ?? 0) > 0 && (
        <div className="flex flex-wrap gap-2 mb-4">
          {[
            { k: 'p50 handler', v: summary.p50_handler_ms },
            { k: 'p95 handler', v: summary.p95_handler_ms },
            { k: 'p50 LLM', v: summary.p50_llm_ms },
            { k: 'p50 TTS', v: summary.p50_tts_ms },
            { k: 'p50 prompt', v: summary.p50_prompt_ms },
            { k: 'p50 turn gap', v: summary.p50_gap_since_agent_ms },
            { k: 'p50 input wait', v: summary.p50_input_wait_ms },
            { k: 'p50 Chris audio', v: summary.p50_agent_audio_ms },
            { k: 'p50 EOS→audio', v: summary.p50_eos_to_first_audio_ms },
            { k: 'p95 EOS→audio', v: summary.p95_eos_to_first_audio_ms },
            { k: 'p50 first LLM token', v: summary.p50_first_llm_token_ms },
            { k: 'p50 first TTS chunk', v: summary.p50_first_tts_chunk_ms },
            { k: 'p95 first LLM token', v: summary.p95_first_llm_token_ms },
            { k: 'p95 first TTS chunk', v: summary.p95_first_tts_chunk_ms },
            { k: 'p95 STT', v: summary.p95_stt_ms },
            { k: 'p95 LLM', v: summary.p95_llm_ms },
            { k: 'p95 TTS', v: summary.p95_tts_ms },
            { k: 'p50 reply→audio fetch', v: summary.p50_webhook_to_audio_fetch_ms },
            { k: 'p50 Gemini request', v: summary.p50_llm_request_ms },
            { k: 'p50 audio fetch delay', v: summary.p50_audio_fetch_delay_ms },
            { k: 'p50 caller start', v: summary.p50_caller_start_ms },
            { k: 'p50 caller speaking', v: summary.p50_caller_speaking_ms },
            { k: 'p50 speech end→webhook', v: summary.p50_speech_end_to_webhook_ms },
            { k: 'p95 reply→audio fetch', v: summary.p95_webhook_to_audio_fetch_ms },
          ].map(({ k, v }) =>
            v != null && v > 0 ? (
              <span key={k} className="text-[10px] font-medium bg-white border border-brand-100 rounded-md px-2 py-1 text-brand-900">
                {k}: {Math.round(v)}ms
              </span>
            ) : null
          )}
        </div>
      )}

      <div className="space-y-3 max-h-64 overflow-y-auto scrollbar-thin pr-1">
        {rows.map((r, i) => (
          <div key={`${r.turnId}-${i}`} className="text-[11px] border border-slate-100 rounded-lg bg-white/80 p-2.5">
            <div className="font-semibold text-slate-800 mb-2 truncate">{r.label}</div>
            {r.eosToAudio != null && r.eosToAudio > 0 && (
              <div className="flex items-center gap-2 mb-1">
                <span className="w-14 shrink-0 text-slate-500">EOS→audio</span>
                <Bar value={r.eosToAudio} max={maxBar} color="bg-rose-500" />
                <span className="w-12 text-right tabular-nums">{Math.round(r.eosToAudio)}ms</span>
              </div>
            )}
            {r.gap != null && r.gap > 0 && (
              <div className="flex items-center gap-2 mb-1">
                <span className="w-14 shrink-0 text-slate-500">Turn gap</span>
                <Bar value={r.gap} max={maxBar} color="bg-amber-500" />
                <span className="w-12 text-right tabular-nums">{Math.round(r.gap)}ms</span>
              </div>
            )}
            {r.agentAudio != null && r.agentAudio > 0 && (
              <div className="flex items-center gap-2 mb-1">
                <span className="w-14 shrink-0 text-slate-500">Audio</span>
                <Bar value={r.agentAudio} max={maxBar} color="bg-orange-400" />
                <span className="w-12 text-right tabular-nums">{Math.round(r.agentAudio)}ms</span>
              </div>
            )}
            {r.inputWait != null && r.inputWait > 0 && (
              <div className="flex items-center gap-2 mb-1">
                <span className="w-14 shrink-0 text-slate-500">Input</span>
                <Bar value={r.inputWait} max={maxBar} color="bg-yellow-500" />
                <span className="w-12 text-right tabular-nums">{Math.round(r.inputWait)}ms</span>
              </div>
            )}
            {r.llm != null && r.llm > 0 && (
              <div className="flex items-center gap-2 mb-1">
                <span className="w-14 shrink-0 text-slate-500">LLM</span>
                <Bar value={r.llm} max={maxBar} color="bg-slate-500" />
                <span className="w-12 text-right tabular-nums">{Math.round(r.llm)}ms</span>
              </div>
            )}
            {r.prompt != null && r.prompt > 0 && (
              <div className="flex items-center gap-2 mb-1">
                <span className="w-14 shrink-0 text-slate-500">Prompt</span>
                <Bar value={r.prompt} max={maxBar} color="bg-sky-500" />
                <span className="w-12 text-right tabular-nums">{Math.round(r.prompt)}ms</span>
              </div>
            )}
            {r.tts != null && r.tts > 0 && (
              <div className="flex items-center gap-2 mb-1">
                <span className="w-14 shrink-0 text-slate-500">TTS</span>
                <Bar value={r.tts} max={maxBar} color="bg-emerald-500" />
                <span className="w-12 text-right tabular-nums">{Math.round(r.tts)}ms</span>
              </div>
            )}
            {r.audioFetch != null && r.audioFetch > 0 && (
              <div className="flex items-center gap-2 mb-1">
                <span className="w-14 shrink-0 text-slate-500">To audio</span>
                <Bar value={r.audioFetch} max={maxBar} color="bg-rose-400" />
                <span className="w-12 text-right tabular-nums">{Math.round(r.audioFetch)}ms</span>
              </div>
            )}
            {r.fetchWait != null && r.fetchWait > 0 && (
              <div className="flex items-center gap-2 mb-1">
                <span className="w-14 shrink-0 text-slate-500">Fetch wait</span>
                <Bar value={r.fetchWait} max={maxBar} color="bg-orange-400" />
                <span className="w-12 text-right tabular-nums">{Math.round(r.fetchWait)}ms</span>
              </div>
            )}
            {r.handler != null && (
              <div className="flex items-center gap-2 mb-1">
                <span className="w-14 shrink-0 text-slate-500">Total</span>
                <Bar value={r.handler} max={maxBar} color="bg-brand-600" />
                <span className="w-12 text-right tabular-nums">{Math.round(r.handler)}ms</span>
              </div>
            )}
            <div className="flex flex-wrap gap-x-2 gap-y-0.5 mt-1.5 text-[10px] text-slate-500">
              {r.path && <span>path: {r.path}</span>}
              {r.sttConf != null && <span>STT conf: {(r.sttConf * 100).toFixed(0)}%</span>}
              {r.backchannelWait != null && r.backchannelWait > 0 && (
                <span>backchannel→main: {Math.round(r.backchannelWait)}ms</span>
              )}
              {r.verbalSource && <span>line: {r.verbalSource}</span>}
              {r.verbalAck && <span>ack: {r.verbalAck}</span>}
              {r.endpointing != null && <span>endpoint: {Math.round(r.endpointing)}ms</span>}
              {r.sttFinal != null && <span>EOS→STT: {Math.round(r.sttFinal)}ms</span>}
              {r.firstLlmToken != null && <span>first LLM: {Math.round(r.firstLlmToken)}ms</span>}
              {r.firstTtsChunk != null && <span>first TTS: {Math.round(r.firstTtsChunk)}ms</span>}
              {r.orchestration != null && <span>overhead: {Math.round(r.orchestration)}ms</span>}
              {r.model && <span>model: {r.model}{r.slm ? ' (SLM)' : ''}</span>}
              {r.voice && <span>voice: {r.voice}{r.ttsFallback ? ' (fallback)' : ''}</span>}
              {r.deferred && <span>deferred (silent hold)</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
