import { useEffect, useRef, useState } from 'react'
import { Phone, PhoneOff, Play, Building2, Hash } from 'lucide-react'
import Button from './ui/Button'
import Badge from './ui/Badge'

interface DemoLine {
  role: 'agent' | 'customer'
  text: string
}

const DEMO_SCRIPT: DemoLine[] = [
  { role: 'agent', text: 'Good afternoon. This is Chris from Apollo Hospital, following up on claim 78462901 for Mr. Suresh Rao. Could you confirm the current status and any documents still required?' },
  { role: 'customer', text: 'The claim is pending medical review. Denial CO-16 was raised for a missing cath report. Has that been uploaded?' },
  { role: 'agent', text: 'We will upload the cath report within the hour. What is the expected timeline once documents are received?' },
  { role: 'customer', text: 'Medical review typically completes within 4 hours for cardiac cases after upload.' },
  { role: 'agent', text: 'Are any other documents needed besides the cath report?' },
  { role: 'customer', text: 'Only the cath report is pending. Everything else is on file.' },
  { role: 'agent', text: 'Thank you. I will log this as pending review and schedule follow-up if not resolved by tomorrow.' },
]

const PREVIEW_LINES = DEMO_SCRIPT.slice(0, 2)

const OUTCOME_TAGS = [
  { label: 'Denial CO-16', color: 'bg-rose-50 text-rose-800 ring-rose-200' },
  { label: 'Cath report pending', color: 'bg-amber-50 text-amber-800 ring-amber-200' },
  { label: 'Review in 4 hrs', color: 'bg-sky-50 text-sky-800 ring-sky-200' },
]

export default function DemoCall() {
  const [active, setActive] = useState(false)
  const [lines, setLines] = useState<DemoLine[]>([])
  const [status, setStatus] = useState<'idle' | 'ringing' | 'connected' | 'ended'>('idle')
  const scrollRef = useRef<HTMLDivElement>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout>[]>([])

  const clearTimers = () => {
    timerRef.current.forEach(clearTimeout)
    timerRef.current = []
  }

  const startDemo = () => {
    clearTimers()
    setActive(true)
    setLines([])
    setStatus('ringing')
    timerRef.current.push(
      setTimeout(() => setStatus('connected'), 1200),
      ...DEMO_SCRIPT.map((line, i) =>
        setTimeout(() => setLines((prev) => [...prev, line]), 1800 + i * 2400)
      ),
      setTimeout(() => setStatus('ended'), 1800 + DEMO_SCRIPT.length * 2400 + 800)
    )
  }

  const endDemo = () => {
    clearTimers()
    setActive(false)
    setStatus('idle')
    setLines([])
  }

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [lines])

  useEffect(() => () => clearTimers(), [])

  const displayLines = active ? lines : PREVIEW_LINES
  const isPreview = !active && status === 'idle'

  return (
    <div className="rounded-xl overflow-hidden bg-white shadow-sm ring-1 ring-slate-200">
      {/* Window chrome */}
      <div className="relative flex items-center gap-3 px-4 py-2.5 bg-slate-50 border-b border-slate-200/80">
        <div className="flex gap-1.5 shrink-0">
          <span className="w-2.5 h-2.5 rounded-full bg-slate-300" />
          <span className="w-2.5 h-2.5 rounded-full bg-slate-400" />
          <span className="w-2.5 h-2.5 rounded-full bg-slate-500" />
        </div>
        <p className="text-[11px] font-medium text-slate-500 flex-1 text-center">
          ClaimsVoice · Sample follow-up
        </p>
        <div className="w-14 shrink-0 flex justify-end">
          {status === 'connected' && <Badge variant="success" dot>Live</Badge>}
        </div>
      </div>

      <div className="flex flex-col sm:flex-row min-h-[340px]">
        {/* Side panel — claim context */}
        <aside className="sm:w-[168px] shrink-0 p-4 bg-slate-50/80 border-b sm:border-b-0 sm:border-r border-slate-200/80 flex flex-col gap-4">
          <div>
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2">Claim</p>
            <p className="text-sm font-bold text-slate-900 leading-snug">Mr. Suresh Rao</p>
            <p className="text-xs text-slate-500 mt-0.5 flex items-center gap-1">
              <Hash className="w-3 h-3" /> 78462901
            </p>
            <p className="text-[11px] text-slate-500 mt-1 flex items-center gap-1">
              <Building2 className="w-3 h-3 shrink-0" /> Apollo Hospital
            </p>
            <span className="inline-block mt-2 text-[10px] font-semibold px-2 py-0.5 rounded-md bg-slate-100 text-slate-800">
              Pending
            </span>
          </div>

          <div>
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2">On the call</p>
            <div className="flex items-center -space-x-2">
              <div className="w-8 h-8 rounded-full bg-brand-600 text-white text-xs font-bold flex items-center justify-center ring-2 ring-white" title="Chris">
                C
              </div>
              <div className="w-8 h-8 rounded-full bg-slate-500 text-white text-[10px] font-bold flex items-center justify-center ring-2 ring-white" title="Insurance rep">
                IR
              </div>
            </div>
            <p className="text-[10px] text-slate-500 mt-2">Chris ↔ Payer rep</p>
          </div>

          <div className="hidden sm:block mt-auto">
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2">Captured</p>
            <div className="flex flex-col gap-1.5">
              {OUTCOME_TAGS.map((t) => (
                <span key={t.label} className={`text-[10px] font-medium px-2 py-1 rounded-md ring-1 ${t.color}`}>
                  {t.label}
                </span>
              ))}
            </div>
          </div>
        </aside>

        {/* Chat panel */}
        <div className="flex-1 flex flex-col min-w-0">
          <div
            ref={scrollRef}
            className="flex-1 h-52 sm:h-auto sm:min-h-[240px] overflow-y-auto p-4 space-y-4 scrollbar-thin bg-[linear-gradient(180deg,#fff_0%,#f8fafc_100%)]"
          >
            {status === 'ringing' && lines.length === 0 && (
              <div className="flex flex-col items-center justify-center h-full py-10 text-center">
                <div className="w-10 h-10 rounded-full bg-brand-100 flex items-center justify-center mb-2 animate-pulse-soft">
                  <Phone className="w-4 h-4 text-brand-600" />
                </div>
                <p className="text-sm font-medium text-slate-600">Calling payer helpline…</p>
              </div>
            )}

            {displayLines.map((line, i) => {
              const isAgent = line.role === 'agent'
              return (
                <div key={i} className={`flex gap-2.5 ${isAgent ? '' : 'flex-row-reverse'}`}>
                  <div
                    className={`w-7 h-7 rounded-full shrink-0 flex items-center justify-center text-[10px] font-bold text-white ${
                      isAgent ? 'bg-brand-600' : 'bg-slate-500'
                    }`}
                  >
                    {isAgent ? 'C' : 'IR'}
                  </div>
                  <div className={`max-w-[85%] min-w-0 ${isAgent ? '' : 'text-right'}`}>
                    <div
                      className={`inline-block text-left rounded-2xl px-3.5 py-2.5 text-[13px] leading-relaxed ${
                        isAgent
                          ? 'bg-white text-slate-700 shadow-sm ring-1 ring-slate-200/80 rounded-tl-sm'
                          : 'bg-brand-600 text-white shadow-sm rounded-tr-sm'
                      }`}
                    >
                      {line.text}
                    </div>
                  </div>
                </div>
              )
            })}

            {isPreview && (
              <p className="text-center text-[11px] text-slate-400 pt-2">
                Preview transcript · tap play for full call
              </p>
            )}
          </div>

          {/* Mobile outcome tags */}
          <div className="sm:hidden px-4 pb-2 flex flex-wrap gap-1.5">
            {OUTCOME_TAGS.map((t) => (
              <span key={t.label} className={`text-[10px] font-medium px-2 py-0.5 rounded-md ring-1 ${t.color}`}>
                {t.label}
              </span>
            ))}
          </div>

          {/* Action bar */}
          <div className="px-4 py-3 border-t border-slate-100 bg-white flex items-center justify-between gap-3">
            <p className="text-[11px] text-slate-400 hidden sm:block">
              {active ? 'Demo in progress' : 'Interactive sample'}
            </p>
            {!active ? (
              <Button
                onClick={startDemo}
                variant="primary"
                size="md"
                className="w-full sm:w-auto sm:ml-auto"
                icon={<Play className="w-4 h-4 fill-current" />}
              >
                Play sample call
              </Button>
            ) : (
              <Button
                onClick={endDemo}
                variant="secondary"
                size="md"
                className="w-full sm:w-auto sm:ml-auto"
                icon={<PhoneOff className="w-4 h-4" />}
              >
                End preview
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
