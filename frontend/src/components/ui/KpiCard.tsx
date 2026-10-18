import type { LucideIcon } from 'lucide-react'

interface KpiCardProps {
  label: string
  value: string | number
  change?: string
  changeType?: 'positive' | 'negative' | 'neutral'
  icon: LucideIcon
  index?: number
}

const CARD_THEME = {
  card: 'bg-white border-slate-200 shadow-sm',
  iconBg: 'bg-slate-100 ring-slate-200',
  iconColor: 'text-brand-800',
  labelColor: 'text-slate-500',
} as const

export default function KpiCard({ label, value, change, changeType = 'neutral', icon: Icon, index = 0 }: KpiCardProps) {
  const changeColors = {
    positive: 'text-emerald-800 font-semibold',
    negative: 'text-red-700 font-semibold',
    neutral: 'text-slate-700 font-medium',
  }

  const theme = CARD_THEME

  return (
    <div
      className={`rounded-2xl p-6 border transition-all duration-300 hover:-translate-y-0.5 hover:shadow-lg opacity-0 animate-fade-up ${theme.card}`}
      style={{ animationDelay: `${index * 80}ms`, animationFillMode: 'forwards' }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className={`text-xs font-bold uppercase tracking-wide ${theme.labelColor}`}>{label}</p>
          <p className="text-3xl font-bold text-slate-900 mt-2 tracking-tight tabular-nums">{value}</p>
          {change && <p className={`text-xs mt-2 ${changeColors[changeType]}`}>{change}</p>}
        </div>
        <div className={`w-11 h-11 rounded-xl ring-1 flex items-center justify-center shrink-0 ${theme.iconBg}`}>
          <Icon className={`w-5 h-5 ${theme.iconColor}`} strokeWidth={1.75} />
        </div>
      </div>
    </div>
  )
}
