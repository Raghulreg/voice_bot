import { useEffect, useRef, useState } from 'react'
import { User, Clock, Phone } from 'lucide-react'
import { fetchLiveGreeting, sendLiveAudio, type CallRecord } from '../api'
import { sharedAudioContext } from '../utils/micUnlock'
import { isCallTerminal } from '../utils/callProgress'

let activeClip: HTMLAudioElement | null = null

function pcm16k(input: Float32Array, sampleRate: number): ArrayBuffer {
  const ratio = sampleRate / 16000
  const length = Math.max(1, Math.floor(input.length / Math.max(ratio, 1)))
  const out = new Int16Array(length)
  for (let i = 0; i < length; i += 1) {
    const start = Math.floor(i * ratio)
    const end = Math.min(input.length, Math.max(start + 1, Math.floor((i + 1) * ratio)))
    let sum = 0
    let count = 0
    for (let j = start; j < end; j += 1) {
      sum += input[j]
      count += 1
    }
    const sample = Math.max(-1, Math.min(1, count ? sum / count : 0))
    out[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff
  }
  return out.buffer
}

function floatRms(input: Float32Array) {
  let sum = 0
  for (let i = 0; i < input.length; i += 1) sum += input[i] * input[i]
  return Math.sqrt(sum / input.length)
}

function mulawToFloat(bytes: Uint8Array) {
  const out = new Float32Array(bytes.length)
  for (let i = 0; i < bytes.length; i += 1) {
    let u = ~bytes[i] & 0xff
    const sign = u & 0x80
    const exponent = (u >> 4) & 0x07
    const mantissa = u & 0x0f
    let sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    if (sign) sample = -sample
    out[i] = Math.max(-1, Math.min(1, sample / 32768))
  }
  return out
}

function playWav(b64: string): Promise<void> {
  return new Promise((resolve) => {
    if (activeClip) {
      activeClip.pause()
      activeClip.src = ''
      activeClip = null
    }
    if (!b64) {
      resolve()
      return
    }
    const audio = new Audio(`data:audio/wav;base64,${b64}`)
    activeClip = audio
    const finish = () => {
      if (activeClip === audio) activeClip = null
      resolve()
    }
    audio.onended = finish
    audio.onerror = finish
    void audio.play().then(() => undefined, finish)
  })
}

interface Props {
  agentName: string
  patientName: string
  hospitalName: string
  claimId: string
  claimType: string
  payerName: string
  phone: string
  activeCall: CallRecord | null
  callLive: boolean
  initiating: boolean
  callStartedAt: number | null
  result: { type: 'success' | 'error'; message: string } | null
  tpaProfile?: string
}

function Waveform() {
  const delays = [0, 0.1, 0.2, 0.15, 0.25, 0.05, 0.18]
  return (
    <div className="flex items-center justify-center gap-1 h-6">
      {delays.map((d, i) => (
        <div key={i} className="waveform-bar w-1 h-4 rounded-full" style={{ animationDelay: `${d}s` }} />
      ))}
    </div>
  )
}

export default function CallExperiencePanel({
  agentName,
  patientName,
  hospitalName,
  claimId,
  claimType,
  payerName,
  phone,
  activeCall,
  callLive,
  initiating,
  callStartedAt,
  result,
  tpaProfile,
}: Props) {
  const [elapsed, setElapsed] = useState(0)
  const [micNote, setMicNote] = useState<string | null>(null)
  const transcriptEndRef = useRef<HTMLDivElement>(null)
  const playingRef = useRef(false)

  const showTimer = !!(callLive || initiating || (activeCall && callStartedAt))
  const callEnded = !!(activeCall && isCallTerminal(activeCall.status))
  const connected = callLive || callEnded || !!(activeCall?.transcript?.length)

  useEffect(() => {
    if (!showTimer || !callStartedAt) {
      if (!callEnded) setElapsed(0)
      return
    }

    if (callEnded && activeCall?.duration_seconds) {
      setElapsed(Math.round(activeCall.duration_seconds))
      return
    }

    const tick = () => setElapsed(Math.floor((Date.now() - callStartedAt) / 1000))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [showTimer, callStartedAt, callEnded, activeCall?.duration_seconds])

  useEffect(() => {
    const callId = activeCall?.call_id
    if (!callLive || !callId) return
    let cancelled = false
    let meter: number | null = null
    let recorder: MediaRecorder | null = null
    let stream: MediaStream | null = null
    playingRef.current = false

    const level = (analyser: AnalyserNode, buf: Uint8Array) => {
      analyser.getByteTimeDomainData(buf as Uint8Array<ArrayBuffer>)
      let sum = 0
      for (let i = 0; i < buf.length; i += 1) {
        const v = (buf[i] - 128) / 128
        sum += v * v
      }
      return Math.sqrt(sum / buf.length)
    }

    const listen = (analyser: AnalyserNode, buf: Uint8Array) => {
      if (cancelled || playingRef.current || !stream) return
      let noise = 0.012
      let samples = 0
      const arm = () => {
        if (cancelled || playingRef.current) return
        const rms = level(analyser, buf)
        if (samples < 5) {
          noise = noise * 0.7 + rms * 0.3
          samples += 1
          meter = window.setTimeout(arm, 40)
          return
        }
        if (rms >= Math.max(0.006, noise * 1.45)) {
          startClip(Math.max(0.005, noise * 1.25))
          return
        }
        meter = window.setTimeout(arm, 40)
      }
      const startClip = (speechAt: number) => {
        const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
          ? 'audio/webm;codecs=opus'
          : 'audio/webm'
        const chunks: Blob[] = []
        const rec = new MediaRecorder(stream as MediaStream, { mimeType: mime })
        recorder = rec
        rec.ondataavailable = (event) => {
          if (event.data.size) chunks.push(event.data)
        }
        rec.onstop = () => {
          const clip = new Blob(chunks, { type: mime })
          if (cancelled) return
          if (clip.size < 1200) {
            window.setTimeout(() => listen(analyser, buf), 120)
            return
          }
          playingRef.current = true
          setMicNote('One moment.')
          void sendLiveAudio(callId, clip)
            .then(async (res) => {
              if (cancelled) return
              if (!res.heard) {
                setMicNote('I missed that. Say it once more.')
                return
              }
              await playWav(res.audio_base64)
            })
            .catch(() => {
              if (!cancelled) setMicNote('I missed that. Say it once more.')
            })
            .finally(() => {
              playingRef.current = false
              if (!cancelled) {
                setMicNote('Your turn.')
                window.setTimeout(() => listen(analyser, buf), 160)
              }
            })
        }
        rec.start()
        setMicNote('Hearing you.')
        let quietMs = 0
        let speechMs = 0
        let last = performance.now()
        const watch = () => {
          if (cancelled || playingRef.current || rec.state !== 'recording') return
          const now = performance.now()
          const dt = now - last
          last = now
          if (level(analyser, buf) >= speechAt) {
            speechMs += dt
            quietMs = 0
          } else {
            quietMs += dt
          }
          if ((speechMs > 220 && quietMs > 380) || speechMs > 8000) {
            rec.stop()
            return
          }
          meter = window.setTimeout(watch, 40)
        }
        meter = window.setTimeout(watch, 40)
      }
      meter = window.setTimeout(arm, 40)
    }

    const talkUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/calls/${callId}/live-talk`
    const ws = new WebSocket(talkUrl)
    ws.binaryType = 'arraybuffer'
    let streaming = false
    let mode: 'arm' | 'talk' | 'hold' = 'hold'
    let listenGeneration = 0
    let lineFinished = true
    let noise = 0.012
    let samples = 0
    let speechMs = 0
    let quietMs = 0
    let lastTick = performance.now()
    let listenAfter = 0
    const preroll: ArrayBuffer[] = []
    let processor: ScriptProcessorNode | null = null
    let audioCtx: AudioContext | null = null
    let speechEndedAt = 0
    let firstAudible = false
    let bargeMs = 0
    const playback = {
      nextAt: 0,
      sources: [] as AudioBufferSourceNode[],
    }

    const stopPlayback = () => {
      for (const src of playback.sources) {
        try { src.stop() } catch { /* already finished */ }
      }
      playback.sources = []
      playback.nextAt = 0
    }

    const enqueueMulaw = (data: ArrayBuffer) => {
      if (!audioCtx) return
      const pcm = mulawToFloat(new Uint8Array(data))
      if (!pcm.length) return
      const clip = audioCtx.createBuffer(1, pcm.length, 8000)
      clip.getChannelData(0).set(pcm)
      const src = audioCtx.createBufferSource()
      src.buffer = clip
      src.connect(audioCtx.destination)
      const now = audioCtx.currentTime
      if (playback.nextAt < now + 0.02) playback.nextAt = now + 0.04
      src.start(playback.nextAt)
      playback.nextAt += clip.duration
      playback.sources.push(src)
      src.onended = () => {
        playback.sources = playback.sources.filter((item) => item !== src)
      }
      if (!firstAudible) {
        firstAudible = true
        console.info('VOICE first_playback_ms', Math.round(performance.now() - speechEndedAt))
      }
    }

    const ready = new Promise<boolean>((resolve) => {
      let settled = false
      const finish = (ok: boolean) => {
        if (settled) return
        settled = true
        window.clearTimeout(timer)
        resolve(ok)
      }
      const timer = window.setTimeout(() => finish(false), 8000)
      ws.onerror = () => finish(false)
      ws.onclose = () => finish(false)
      ws.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
          playingRef.current = true
          enqueueMulaw(event.data)
          return
        }
        if (typeof event.data !== 'string') return
        let msg: { type?: string; audio_base64?: string; heard?: string }
        try {
          msg = JSON.parse(event.data) as { type?: string; audio_base64?: string; heard?: string }
        } catch {
          return
        }
        if (msg.type === 'ready') finish(true)
        if (msg.type === 'fallback') finish(false)
        if (msg.type === 'audio_start') {
          lineFinished = false
          playingRef.current = true
          setMicNote('Speaking.')
          return
        }
        if (msg.type === 'audio_end') {
          const mine = listenGeneration
          lineFinished = true
          const waitMs = audioCtx
            ? Math.max(0, (playback.nextAt - audioCtx.currentTime) * 1000)
            : 0
          window.setTimeout(() => {
            if (cancelled || listenGeneration !== mine) return
            playingRef.current = false
            mode = 'arm'
            preroll.length = 0
            listenAfter = performance.now() + 120
            speechMs = 0
            quietMs = 0
            bargeMs = 0
            firstAudible = false
            if (!msg.heard) setMicNote('I missed that. Say it once more.')
            else setMicNote('Your turn.')
          }, waitMs + 80)
          return
        }
        if (msg.type !== 'reply') return
        void (async () => {
          if (cancelled) return
          if (!msg.heard) setMicNote('I missed that. Say it once more.')
          else await playWav(msg.audio_base64 || '')
          playingRef.current = false
          mode = 'arm'
          speechMs = 0
          quietMs = 0
          if (!cancelled) setMicNote('Your turn.')
        })()
      }
    })

    void (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: true,
            channelCount: 1,
          },
        })
      } catch {
        if (!cancelled) setMicNote('Allow the microphone so this page can hear you.')
        ws.close()
        return
      }
      if (cancelled) {
        stream.getTracks().forEach((track) => track.stop())
        ws.close()
        return
      }
      audioCtx = sharedAudioContext()
      await audioCtx.resume().catch(() => undefined)
      if (audioCtx.state !== 'running') {
        setMicNote('Click once so I can hear you.')
        await new Promise<void>((resolve) => {
          let settled = false
          const done = () => {
            if (settled) return
            settled = true
            window.removeEventListener('pointerdown', wake)
            window.clearInterval(timer)
            resolve()
          }
          const wake = () => {
            void audioCtx?.resume().then(() => {
              if (cancelled || audioCtx?.state === 'running') done()
            })
          }
          const timer = window.setInterval(() => {
            if (cancelled || audioCtx?.state === 'running') done()
          }, 200)
          window.addEventListener('pointerdown', wake)
        })
      }
      if (cancelled) return
      const source = audioCtx.createMediaStreamSource(stream)
      const playSameVoice = async (b64: string) => {
        if (!b64 || !audioCtx) return
        const binary = atob(b64)
        const bytes = new Uint8Array(binary.length)
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
        const pcm = mulawToFloat(bytes)
        if (!pcm.length) return
        const clip = audioCtx.createBuffer(1, pcm.length, 8000)
        clip.getChannelData(0).set(pcm)
        const src = audioCtx.createBufferSource()
        src.buffer = clip
        src.connect(audioCtx.destination)
        playingRef.current = true
        playback.sources.push(src)
        src.start()
        await new Promise<void>((resolve) => {
          src.onended = () => resolve()
        })
        playback.sources = playback.sources.filter((item) => item !== src)
        playingRef.current = false
      }
      setMicNote('Agent is speaking.')
      playingRef.current = true
      try {
        const greet = await fetchLiveGreeting(callId)
        if (cancelled) return
        await playSameVoice(greet.audio_base64)
      } catch {
        if (!cancelled) setMicNote('Opening line did not play. You can still speak.')
      } finally {
        playingRef.current = false
      }
      if (cancelled) return
      streaming = await ready
      if (!streaming || ws.readyState !== WebSocket.OPEN) {
        ws.close()
        const analyser = audioCtx.createAnalyser()
        analyser.fftSize = 512
        source.connect(analyser)
        const buf = new Uint8Array(analyser.fftSize)
        setMicNote('Your turn. Speak, then pause.')
        listen(analyser, buf)
        return
      }
      const node = audioCtx.createScriptProcessor(2048, 1, 1)
      processor = node
      const sink = audioCtx.createGain()
      sink.gain.value = 0
      source.connect(node)
      node.connect(sink)
      sink.connect(audioCtx.destination)
      mode = 'arm'
      setMicNote('Your turn.')
      node.onaudioprocess = (event) => {
        if (cancelled || ws.readyState !== WebSocket.OPEN) return
        const input = event.inputBuffer.getChannelData(0)
        const rms = floatRms(input)
        const now = performance.now()
        const dt = now - lastTick
        lastTick = now
        if (mode === 'hold') {
          const drained = !!audioCtx && audioCtx.currentTime >= playback.nextAt - 0.02
          const gaveUp = now - speechEndedAt > 15000
          if ((lineFinished && drained) || gaveUp) {
            playingRef.current = false
            mode = 'arm'
            preroll.length = 0
            listenAfter = now + 120
            speechMs = 0
            quietMs = 0
            bargeMs = 0
          } else {
            return
          }
        }
        if (playingRef.current) {
          if (rms >= Math.max(0.16, noise * 6)) bargeMs += dt
          else bargeMs = 0
          if (bargeMs > 450) {
            stopPlayback()
            playingRef.current = false
            mode = 'talk'
            listenGeneration += 1
            speechMs = 220
            quietMs = 0
            bargeMs = 0
            firstAudible = false
            setMicNote('Hearing you.')
            ws.send(JSON.stringify({ type: 'interrupt' }))
            ws.send(pcm16k(input, audioCtx?.sampleRate || 48000))
          }
          return
        }
        const pcm = pcm16k(input, audioCtx?.sampleRate || 48000)
        if (mode === 'arm') {
          if (now < listenAfter) return
          preroll.push(pcm)
          if (preroll.length > 8) preroll.shift()
          const loud = rms >= 0.012
          if (samples < 8 && !loud) {
            noise = noise * 0.7 + rms * 0.3
            samples += 1
            return
          }
          if (rms < Math.max(0.006, noise * 1.45)) return
          mode = 'talk'
          listenGeneration += 1
          speechMs = 220
          quietMs = 0
          setMicNote('Hearing you.')
          for (const chunk of preroll) ws.send(chunk)
          return
        }
        ws.send(pcm)
        if (rms >= Math.max(0.005, noise * 1.25)) {
          speechMs += dt
          quietMs = 0
        } else {
          quietMs += dt
        }
        if ((speechMs > 220 && quietMs > 280) || speechMs > 8000) {
          mode = 'hold'
          preroll.length = 0
          lineFinished = false
          playingRef.current = true
          speechEndedAt = performance.now()
          firstAudible = false
          playback.nextAt = 0
          setMicNote('One moment.')
          ws.send(JSON.stringify({ type: 'end' }))
        }
      }
    })()

    return () => {
      cancelled = true
      playingRef.current = false
      if (meter !== null) window.clearTimeout(meter)
      if (recorder && recorder.state === 'recording') recorder.stop()
      if (processor) processor.onaudioprocess = null
      stopPlayback()
      ws.close()
      stream?.getTracks().forEach((track) => track.stop())
      if (activeClip) {
        activeClip.pause()
        activeClip.src = ''
        activeClip = null
      }
    }
  }, [callLive, activeCall?.call_id])

  const openingLine = `Hi, this is ${agentName} calling from ${hospitalName || 'the hospital'} regarding ${claimType || 'a benefits and claims matter'}. Could you help me with that?`

  const transcript = activeCall?.transcript ?? []
  const liveCap = activeCall?.live_transcript
  const liveText = (liveCap?.text || '').trim()
  const showLiveBubble =
    callLive &&
    !!liveText &&
    !liveCap?.committed &&
    !transcript.some((t) => t.role === 'customer' && t.text === liveText)

  const hasClaimContext = !!(patientName || claimId || payerName || phone.trim())
  const showTranscript = hasClaimContext || initiating || callLive || callEnded || !!result
  const displayLines =
    transcript.length > 0
      ? transcript
      : showTranscript && (initiating || callLive)
        ? [{ role: 'agent', text: openingLine }]
        : []

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [transcript.length, displayLines.length, liveText])

  const formatTime = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

  const connectionLabel = initiating
    ? 'Dialing…'
    : callLive
      ? 'Connected'
      : callEnded
        ? 'Ended'
        : '—'

  return (
    <div className="flex flex-col h-full bg-[#fafbfc]">
      <div className="px-5 py-3 bg-white border-b border-slate-200/80 flex items-center justify-between gap-4">
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2">
            <div className={`w-9 h-9 rounded-full flex items-center justify-center ${connected ? 'bg-brand-600 text-white' : 'bg-brand-50 text-brand-700 ring-1 ring-brand-100'}`}>
              <User className="w-4 h-4" />
            </div>
            <div>
              <p className="text-xs font-bold text-slate-900">{agentName}</p>
              <p className="text-[10px] text-slate-500">Your representative</p>
            </div>
          </div>

          <div className="hidden sm:flex flex-col items-center px-4">
            {callLive ? <Waveform /> : <Phone className={`w-4 h-4 ${connected ? 'text-brand-600' : 'text-slate-300'}`} />}
            <p className="text-[10px] text-slate-400 mt-0.5">{connectionLabel}</p>
          </div>

          <div className="flex items-center gap-2">
            <div className={`w-9 h-9 rounded-full flex items-center justify-center ring-1 ${connected ? 'bg-brand-50 text-brand-700 ring-brand-100' : 'bg-slate-100 text-slate-500 ring-slate-200'}`}>
              <User className="w-4 h-4" />
            </div>
            <div>
              <p className="text-xs font-bold text-slate-900 truncate max-w-[120px]">{payerName || 'Insurance'}</p>
              <p className="text-[10px] text-slate-500 truncate max-w-[140px]">
                {tpaProfile ? tpaProfile.replace(' Executive', '') : 'Payer representative'}
              </p>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {callLive && (
            <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-50 text-emerald-700 text-[11px] font-bold ring-1 ring-emerald-200">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
              LIVE
            </span>
          )}
          {showTimer && (
            <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-slate-100 text-slate-700 text-sm font-bold tabular-nums">
              <Clock className="w-3.5 h-3.5 text-slate-400" />
              {formatTime(elapsed)}
            </span>
          )}
        </div>
      </div>

      <div className="flex-1 flex flex-col p-5 min-h-[340px]">
        <h2 className="text-sm font-bold text-slate-900 mb-1">Live Call</h2>
        {callLive && micNote && (
          <p className="text-[11px] text-brand-700 mb-2">{micNote}</p>
        )}
        {tpaProfile && (
          <p className="text-[11px] text-slate-700 mb-2">
            Rep profile · {tpaProfile.replace(' Executive', '')}
          </p>
        )}

        <div className="flex-1 rounded-xl bg-white border border-slate-200/80 shadow-sm overflow-hidden flex flex-col">
          <div className="flex-1 overflow-y-auto p-4 space-y-4 scrollbar-thin">
            {!showTranscript && (
              <div className="h-full flex flex-col items-center justify-center text-center py-12">
                <div className="w-14 h-14 rounded-2xl bg-brand-50 flex items-center justify-center mb-3 ring-1 ring-brand-100">
                  <Phone className="w-6 h-6 text-brand-600" />
                </div>
                <p className="text-sm font-semibold text-slate-800">Ready when you are</p>
                <p className="text-xs text-slate-500 mt-1 max-w-[280px]">
                  Fill in the claim summary above, then start the insurance follow-up call.
                </p>
              </div>
            )}

            {showTranscript && displayLines.map((line, i) => {
              const isAgent = line.role === 'agent'
              const isLast = i === displayLines.length - 1
              const showTyping = callLive && !isAgent && isLast && line.role === 'customer' && !line.text

              return (
                <div key={`${line.role}-${i}`} className={`flex ${isAgent ? 'justify-start' : 'justify-end'}`}>
                  <div className="max-w-[85%]">
                    <p className={`text-[10px] font-bold mb-1.5 uppercase tracking-wide ${isAgent ? 'text-brand-700' : 'text-slate-400 text-right'}`}>
                      {isAgent ? agentName : 'Insurance rep'}
                    </p>
                    <div
                      className={`rounded-2xl px-4 py-3 text-[13px] leading-relaxed ${
                        isAgent
                          ? 'rounded-tl-md bg-slate-50 ring-1 ring-slate-200/60 text-slate-700'
                          : 'rounded-tr-md bg-brand-600 text-white shadow-sm'
                      }`}
                    >
                      {showTyping ? (
                        <span className="inline-flex gap-1.5 py-0.5">
                          <span className="w-2 h-2 rounded-full bg-white/80 animate-pulse-soft" />
                          <span className="w-2 h-2 rounded-full bg-white/80 animate-pulse-soft" style={{ animationDelay: '0.15s' }} />
                          <span className="w-2 h-2 rounded-full bg-white/80 animate-pulse-soft" style={{ animationDelay: '0.3s' }} />
                        </span>
                      ) : (
                        line.text
                      )}
                    </div>
                  </div>
                </div>
              )
            })}

            {showLiveBubble && (
              <div className="flex justify-end">
                <div className="max-w-[85%]">
                  <p className="text-[10px] font-bold text-slate-400 mb-1.5 uppercase tracking-wide text-right">
                    Insurance rep · live
                  </p>
                  <div className="rounded-2xl rounded-tr-md px-4 py-3 bg-brand-500 text-white text-[13px] shadow-sm ring-2 ring-brand-300/60">
                    {liveText}
                  </div>
                </div>
              </div>
            )}

            <div ref={transcriptEndRef} />

            {result && !callLive && (
              <div className={`mx-1 p-3 rounded-lg text-xs font-medium ${result.type === 'success' ? 'bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200' : 'bg-red-50 text-red-800 ring-1 ring-red-200'}`}>
                {result.message}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
