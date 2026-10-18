type BadgeVariant = 'success' | 'warning' | 'info' | 'error' | 'neutral' | 'pending' | 'generating' | 'validated' | 'duplicate'

const styles: Record<BadgeVariant, string> = {
  success: 'bg-emerald-100 text-emerald-800 border border-emerald-200',
  warning: 'bg-amber-100 text-amber-900 border border-amber-200',
  info: 'bg-zinc-100 text-zinc-800 border border-zinc-200',
  error: 'bg-red-100 text-red-800 border border-red-200',
  neutral: 'bg-zinc-100 text-zinc-700 border border-zinc-200',
  pending: 'bg-amber-100 text-amber-900 border border-amber-200',
  generating: 'bg-sky-100 text-sky-900 border border-sky-200',
  validated: 'bg-emerald-100 text-emerald-800 border border-emerald-200',
  duplicate: 'bg-orange-100 text-orange-900 border border-orange-200',
}

const statusMap: Record<string, BadgeVariant> = {
  initiated: 'neutral',
  calling: 'generating',
  ringing: 'generating',
  answered: 'success',
  'in-progress': 'generating',
  completed: 'validated',
  failed: 'error',
  pending: 'pending',
}

interface BadgeProps {
  children: React.ReactNode
  variant?: BadgeVariant
  status?: string
  dot?: boolean
  className?: string
}

export default function Badge({ children, variant, status, dot, className = '' }: BadgeProps) {
  const resolved = variant || (status ? statusMap[status] || 'neutral' : 'neutral')
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md text-xs font-medium ${styles[resolved]} ${className}`}>
      {dot && (
        <span
          className={`w-1.5 h-1.5 rounded-full ${
            resolved === 'success' || resolved === 'validated'
              ? 'bg-emerald-600'
              : resolved === 'error'
                ? 'bg-red-600'
                : resolved === 'generating'
                  ? 'bg-sky-600'
                  : resolved === 'warning' || resolved === 'pending'
                    ? 'bg-amber-600'
                    : 'bg-zinc-500'
          }`}
        />
      )}
      {children}
    </span>
  )
}

export function StatusBadge({ status }: { status: string }) {
  const labels: Record<string, string> = {
    initiated: 'Call started',
    calling: 'Calling',
    ringing: 'Ringing',
    answered: 'Connected',
    'in-progress': 'In progress',
    completed: 'Completed',
    failed: 'Unable to connect',
  }
  const label = labels[status] || status.replace(/-/g, ' ')
  return (
    <Badge status={status} dot>
      <span>{label}</span>
    </Badge>
  )
}
