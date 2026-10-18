import { useEffect, useRef, useState } from 'react'
import { Save, RefreshCw, Play, Volume2, Link2 } from 'lucide-react'
import {
  fetchConversationQuality,
  patchVoiceSettings,
  previewVoiceSample,
  type ConversationQualityPayload,
} from '../api'
import Button from './ui/Button'

function Slider({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string
  value: number
  min: number
  max: number
  step: number
  onChange: (v: number) => void
}) {
  return (
    <label className="block">
      <div className="flex justify-between text-sm mb-1">
        <span className="text-zinc-700">{label}</span>
        <span className="tabular-nums font-medium text-zinc-900">{value.toFixed(2)}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="w-full accent-brand-600"
      />
    </label>
  )
}

export default function ConversationQualityPanel() {
  const [connected, setConnected] = useState<boolean | null>(null)
  const [data, setData] = useState<ConversationQualityPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const [stability, setStability] = useState(0.45)
  const [similarity, setSimilarity] = useState(0.8)
  const [style, setStyle] = useState(0)
  const [speed, setSpeed] = useState(1)
  const [volumeGain, setVolumeGain] = useState(1)
  const [speakerBoost, setSpeakerBoost] = useState(true)
  const [backchannel, setBackchannel] = useState(true)
  const [lowLatency, setLowLatency] = useState(false)
  const [gatherTimeout, setGatherTimeout] = useState('auto')

  const applyVoice = (v: ConversationQualityPayload['voice']) => {
    setStability(v.stability)
    setSimilarity(v.similarity)
    setStyle(v.style)
    setSpeed(v.speed)
    setVolumeGain(v.volume_gain)
    setSpeakerBoost(v.speaker_boost)
    setBackchannel(v.backchannel_enabled)
    setLowLatency(v.low_latency_calls)
    setGatherTimeout(v.twilio_gather_speech_timeout)
  }

  const load = () => {
    setLoading(true)
    setError(null)
    fetchConversationQuality()
      .then((d) => {
        setConnected(true)
        setData(d)
        applyVoice(d.voice)
      })
      .catch((e) => {
        setConnected(false)
        setError(e instanceof Error ? e.message : 'Could not reach backend')
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
    return () => {
      if (audioRef.current) {
        URL.revokeObjectURL(audioRef.current.src)
      }
    }
  }, [])

  const handleSave = async () => {
    setSaving(true)
    setMessage(null)
    try {
      const res = await patchVoiceSettings({
        stability,
        similarity,
        style,
        speed,
        volume_gain: volumeGain,
        speaker_boost: speakerBoost,
        backchannel_enabled: backchannel,
        low_latency_calls: lowLatency,
        twilio_gather_speech_timeout: gatherTimeout,
      })
      setData((prev) => (prev ? { ...prev, voice: res.voice } : prev))
      setMessage('Saved. The next phone line Chris speaks will use these values.')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const handlePreview = async () => {
    setPreviewing(true)
    setMessage(null)
    try {
      await patchVoiceSettings({
        stability,
        similarity,
        style,
        speed,
        volume_gain: volumeGain,
        speaker_boost: speakerBoost,
      })
      const blob = await previewVoiceSample()
      if (audioRef.current) {
        URL.revokeObjectURL(audioRef.current.src)
      }
      const url = URL.createObjectURL(blob)
      audioRef.current = new Audio(url)
      await audioRef.current.play()
      setMessage('Playing preview with current sliders (same API as live calls).')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Preview failed')
    } finally {
      setPreviewing(false)
    }
  }

  if (loading && !data) {
    return <div className="max-w-2xl mx-auto rounded-xl border border-zinc-200 bg-white p-8 text-center text-sm text-zinc-500">Loading…</div>
  }

  if (error && !data) {
    return (
      <div className="max-w-2xl mx-auto rounded-xl border border-amber-200 bg-amber-50 p-8 text-center">
        <p className="font-medium text-zinc-900">Backend not connected</p>
        <p className="text-sm text-zinc-600 mt-2">{error}</p>
        <p className="text-xs text-zinc-500 mt-2">Start backend: python main.py — then refresh.</p>
        <Button variant="secondary" size="sm" className="mt-4" onClick={load}>
          Retry
        </Button>
      </div>
    )
  }

  const voiceOk = data?.voice.voice_id_configured

  return (
    <div className="max-w-2xl mx-auto space-y-6 pb-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-zinc-900">Voice & quality</h1>
          <p className="text-sm text-zinc-500 mt-0.5">Chris phone voice and conversation behavior</p>
        </div>
        <button
          type="button"
          onClick={load}
          className="p-2 text-zinc-500 hover:text-zinc-800 rounded-lg hover:bg-zinc-100"
          aria-label="Refresh"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      <div
        className={`flex items-center gap-2 text-sm rounded-lg px-3 py-2 border ${
          connected ? 'bg-emerald-50 border-emerald-200 text-emerald-900' : 'bg-zinc-50 border-zinc-200'
        }`}
      >
        <Link2 className="w-4 h-4 shrink-0" />
        {connected ? (
          <span>
            Connected to backend — sliders save to <code className="text-xs">.env</code> and apply on the next
            ElevenLabs request (no restart needed).
          </span>
        ) : (
          <span>Not connected</span>
        )}
      </div>

      {/* Voice */}
      <section className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm">
        <div className="flex items-center gap-2 mb-4">
          <Volume2 className="w-4 h-4 text-brand-600" />
          <h2 className="font-semibold text-zinc-900">Chris voice (ElevenLabs)</h2>
        </div>
        {!voiceOk && (
          <p className="text-sm text-amber-800 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2 mb-4">
            Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in backend .env to hear Chris on calls.
          </p>
        )}
        <div className="space-y-4">
          <Slider label="Speed" value={speed} min={0.5} max={1.2} step={0.05} onChange={setSpeed} />
          <Slider label="Stability" value={stability} min={0} max={1} step={0.05} onChange={setStability} />
          <Slider label="Similarity" value={similarity} min={0} max={1} step={0.05} onChange={setSimilarity} />
          <Slider label="Style" value={style} min={0} max={1} step={0.05} onChange={setStyle} />
          <Slider label="Volume (phone)" value={volumeGain} min={0.7} max={1.4} step={0.05} onChange={setVolumeGain} />
          <label className="flex items-center gap-2 text-sm text-zinc-700">
            <input type="checkbox" checked={speakerBoost} onChange={(e) => setSpeakerBoost(e.target.checked)} />
            Speaker boost
          </label>
        </div>
        <div className="flex flex-wrap gap-2 mt-5 pt-4 border-t border-zinc-100">
          <Button variant="primary" size="sm" onClick={handleSave} disabled={saving} icon={<Save className="w-4 h-4" />}>
            {saving ? 'Saving…' : 'Save'}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={handlePreview}
            disabled={previewing || !voiceOk}
            icon={<Play className="w-4 h-4" />}
          >
            {previewing ? 'Generating…' : 'Preview voice'}
          </Button>
        </div>
        {message && <p className="text-xs text-zinc-600 mt-3">{message}</p>}
        <p className="text-xs text-zinc-400 mt-2">
          Speed changes how fast Chris talks on new TTS lines. Already-cached audio on an active call may sound old until
          new lines are generated.
        </p>
      </section>

      {/* Call behavior */}
      <section className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm">
        <h2 className="font-semibold text-zinc-900 mb-3">Call behavior</h2>
        <div className="space-y-3 text-sm text-zinc-700">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={backchannel} onChange={(e) => setBackchannel(e.target.checked)} />
            Short ack while thinking (Right / Got it)
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={lowLatency} onChange={(e) => setLowLatency(e.target.checked)} />
            Fast mode (cached lines, less Gemini variety)
          </label>
          <label className="block">
            <span className="text-zinc-600">Pause before Chris replies</span>
            <select
              value={gatherTimeout}
              onChange={(e) => setGatherTimeout(e.target.value)}
              className="mt-1 w-full border border-zinc-300 rounded-lg px-3 py-2 text-sm bg-white"
            >
              <option value="auto">Auto</option>
              <option value="1">1s</option>
              <option value="2">2s</option>
              <option value="3">3s</option>
            </select>
          </label>
        </div>
        <Button variant="secondary" size="sm" className="mt-4" onClick={handleSave} disabled={saving}>
          Save behavior
        </Button>
      </section>

      {/* Summary for lead — compact */}
      <section className="rounded-xl border border-zinc-200 bg-zinc-50 p-5 text-sm text-zinc-600 space-y-2">
        <p>
          <strong className="text-zinc-800">Verbal variety:</strong> {data?.verbal_habits.reply_variants_per_step} phrasings
          per step; acks rotate (not identical each turn).
        </p>
        <p>
          <strong className="text-zinc-800">Ack policy:</strong> avoids repeating formal Noted / Thanks on the same call.
        </p>
        <p>
          <strong className="text-zinc-800">Turn timing:</strong> open <strong>Call Records</strong> → expand a call → latency
          bars (STT gap, LLM, TTS).
        </p>
      </section>
    </div>
  )
}
