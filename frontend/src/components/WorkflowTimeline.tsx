import { Check } from 'lucide-react'

export const CLAIM_PROGRESS_STEPS = [
  { id: 'received', title: 'Claim Received', short: 'Received' },
  { id: 'calling', title: 'Calling Insurance', short: 'Calling' },
  { id: 'connected', title: 'Connected', short: 'Connected' },
  { id: 'reason', title: 'Reason for Call', short: 'Purpose' },
  { id: 'information', title: 'Information Received', short: 'Details' },
  { id: 'outcome', title: 'Call Outcome', short: 'Outcome' },
]

interface TimelineProps {
  currentStep: number
  suggestion?: string
  compact?: boolean
}

interface HorizontalProps {
  currentStep: number
}

export function HorizontalClaimProgress({ currentStep }: HorizontalProps) {
  return (
    <div className="flex items-center gap-1 overflow-x-auto scrollbar-thin pb-0.5">
      {CLAIM_PROGRESS_STEPS.map((step, i) => {
        const done = i < currentStep
        const active = i === currentStep
        return (
          <div key={step.id} className="flex items-center shrink-0">
            <div className="flex items-center gap-1.5">
              <div
                className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold transition-colors ${
                  done
                    ? 'bg-brand-600 text-white'
                    : active
                      ? 'bg-brand-600 text-white ring-2 ring-brand-200'
                      : 'bg-slate-200 text-slate-500'
                }`}
              >
                {done ? <Check className="w-3 h-3" strokeWidth={3} /> : i + 1}
              </div>
              <span
                className={`text-xs font-medium whitespace-nowrap ${
                  active ? 'text-brand-800' : done ? 'text-slate-700' : 'text-slate-400'
                }`}
              >
                {step.short}
              </span>
            </div>
            {i < CLAIM_PROGRESS_STEPS.length - 1 && (
              <div className={`w-6 sm:w-10 h-px mx-2 ${done ? 'bg-brand-400' : 'bg-slate-200'}`} />
            )}
          </div>
        )
      })}
    </div>
  )
}

export default function WorkflowTimeline({ currentStep, suggestion, compact }: TimelineProps) {
  if (!compact) {
    return (
      <div>
        <HorizontalClaimProgress currentStep={currentStep} />
        {suggestion && (
          <p className="text-xs text-slate-600 mt-3 leading-relaxed">{suggestion}</p>
        )}
      </div>
    )
  }

  const steps = CLAIM_PROGRESS_STEPS.filter(
    (_, i) => i <= Math.max(currentStep + 1, 1) || i === CLAIM_PROGRESS_STEPS.length - 1
  )

  return (
    <div>
      <p className="text-xs font-bold text-slate-800 mb-3">Claim progress</p>
      <div className="space-y-0">
        {steps.map((step, i) => {
          const stepIndex = CLAIM_PROGRESS_STEPS.findIndex((s) => s.id === step.id)
          const done = stepIndex < currentStep
          const active = stepIndex === currentStep
          return (
            <div key={step.id} className="relative flex gap-2.5 pb-3 last:pb-0">
              {i < steps.length - 1 && (
                <div className={`absolute left-[9px] top-5 w-px h-[calc(100%-4px)] ${done ? 'bg-brand-500' : 'bg-slate-200'}`} />
              )}
              <div
                className={`relative z-10 w-[18px] h-[18px] rounded-full flex items-center justify-center shrink-0 ${
                  done ? 'bg-brand-600 text-white' : active ? 'bg-brand-600 text-white ring-2 ring-brand-100' : 'bg-slate-100'
                }`}
              >
                {done ? <Check className="w-2.5 h-2.5" strokeWidth={3} /> : <span className="w-1.5 h-1.5 rounded-full bg-slate-300" />}
              </div>
              <p className={`text-xs font-medium pt-0.5 ${active ? 'text-brand-800' : 'text-slate-600'}`}>{step.title}</p>
            </div>
          )
        })}
      </div>
      {suggestion && <p className="text-[11px] text-slate-500 mt-2 leading-snug">{suggestion}</p>}
    </div>
  )
}
