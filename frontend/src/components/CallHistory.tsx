import { useEffect, useMemo, useState } from 'react'
import {
  RefreshCw,
  Phone,
  Clock,
  Star,
  MessageSquare,
  Download,
  Search,
  ChevronDown,
  ChevronUp,
  CheckCircle2,
  AlertCircle,
  Loader2,
  PhoneCall,
} from 'lucide-react'
import { fetchCalls, downloadCallJson, type CallRecord } from '../api'
import { CardHeader } from './ui/Card'
import { StatusBadge } from './ui/Badge'
import Button from './ui/Button'
import EmptyState, { TableSkeleton } from './ui/EmptyState'
import KpiCard from './ui/KpiCard'
import TurnLatencyBreakdown from './TurnLatencyBreakdown'

type ClaimTypeFilter = 'all' | 'Claim Pending' | 'Claim Completed' | 'Claim Rejected'

const CLAIM_TYPE_OPTIONS: { value: ClaimTypeFilter; label: string }[] = [
  { value: 'all', label: 'All claims' },
  { value: 'Claim Pending', label: 'Claim Pending' },
  { value: 'Claim Completed', label: 'Claim Completed' },
  { value: 'Claim Rejected', label: 'Claim Rejected' },
]

function matchesClaimTypeFilter(call: CallRecord, filter: ClaimTypeFilter): boolean {
  if (filter === 'all') return true
  const claimType = (call.claim_type || '').toLowerCase()
  if (filter === 'Claim Pending') {
    return claimType.includes('pending')
  }
  if (filter === 'Claim Rejected') {
    return claimType.includes('reject') || claimType.includes('denied')
  }
  if (filter === 'Claim Completed') {
    return claimType.includes('completed') || claimType.includes('approved')
  }
  return true
}

const STATUS_CARD_STYLES: Record<string, { border: string; bg: string; icon: string; iconBg: string }> = {
  completed: {
    border: 'border-l-emerald-500',
    bg: 'bg-white',
    icon: 'text-emerald-700',
    iconBg: 'bg-emerald-500/15 border-emerald-200',
  },
  'in-progress': {
    border: 'border-l-sky-500',
    bg: 'bg-white',
    icon: 'text-sky-700',
    iconBg: 'bg-sky-500/15 border-sky-200',
  },
  calling: {
    border: 'border-l-slate-500',
    bg: 'bg-white',
    icon: 'text-slate-700',
    iconBg: 'bg-slate-500/15 border-slate-200',
  },
  ringing: {
    border: 'border-l-slate-500',
    bg: 'bg-white',
    icon: 'text-slate-700',
    iconBg: 'bg-slate-500/15 border-slate-200',
  },
  answered: {
    border: 'border-l-brand-500',
    bg: 'bg-white',
    icon: 'text-brand-700',
    iconBg: 'bg-brand-500/15 border-brand-200',
  },
  failed: {
    border: 'border-l-red-500',
    bg: 'bg-white',
    icon: 'text-red-700',
    iconBg: 'bg-red-500/15 border-red-200',
  },
  initiated: {
    border: 'border-l-amber-500',
    bg: 'bg-white',
    icon: 'text-amber-700',
    iconBg: 'bg-amber-500/15 border-amber-200',
  },
}

const DEFAULT_CARD_STYLE = {
  border: 'border-l-zinc-400',
  bg: 'bg-white',
  icon: 'text-brand-800',
  iconBg: 'bg-brand-500/15 border-brand-200',
}

function formatCallDate(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

function cardStyle(status: string) {
  return STATUS_CARD_STYLES[status] ?? DEFAULT_CARD_STYLE
}

export default function CallHistory() {
  const [calls, setCalls] = useState<CallRecord[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [claimTypeFilter, setClaimTypeFilter] = useState<ClaimTypeFilter>('all')
  const [page, setPage] = useState(0)
  const pageSize = 8

  const load = () => {
    setLoading(true)
    setLoadError(null)
    fetchCalls()
      .then((c) => setCalls(c.sort((a, b) => b.created_at - a.created_at)))
      .catch((err) => {
        console.error(err)
        setLoadError('Could not reach the backend. Start it with python main.py in the backend folder, then retry.')
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
    const interval = setInterval(load, 5000)
    return () => clearInterval(interval)
  }, [])

  const stats = useMemo(() => {
    const completed = calls.filter((c) => c.status === 'completed').length
    const active = calls.filter((c) => ['in-progress', 'calling', 'ringing', 'answered'].includes(c.status)).length
    const failed = calls.filter((c) => c.status === 'failed').length
    return { total: calls.length, completed, active, failed }
  }, [calls])

  const filtered = useMemo(() => {
    let list = calls.filter((c) => matchesClaimTypeFilter(c, claimTypeFilter))
    if (search.trim()) {
      const q = search.toLowerCase()
      list = list.filter(
        (c) =>
          c.claim_id?.toLowerCase().includes(q) ||
          c.patient_name?.toLowerCase().includes(q) ||
          c.hospital_name?.toLowerCase().includes(q) ||
          c.customer_phone.includes(q)
      )
    }
    return list.sort((a, b) => b.created_at - a.created_at)
  }, [calls, search, claimTypeFilter])

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const paginated = filtered.slice(page * pageSize, (page + 1) * pageSize)

  return (
    <div className="space-y-8">
      <div className="glass-panel px-6 py-5 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 animate-fade-up">
        <div>
          <h1 className="text-xl font-bold text-content-primary tracking-tight">Call Records</h1>
          <p className="text-sm text-content-muted mt-0.5">Review past follow-up calls, outcomes, and conversation notes</p>
        </div>
        <Button variant="secondary" size="sm" onClick={load} disabled={loading} icon={<RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />}>
          Refresh
        </Button>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard label="Total calls" value={loading && calls.length === 0 ? '—' : stats.total} change="All recorded follow-ups" icon={PhoneCall} index={0} />
        <KpiCard label="Completed" value={loading && calls.length === 0 ? '—' : stats.completed} change="Resolved conversations" changeType="positive" icon={CheckCircle2} index={1} />
        <KpiCard label="Active" value={loading && calls.length === 0 ? '—' : stats.active} change="In progress or ringing" icon={Loader2} index={2} />
        <KpiCard label="Failed" value={loading && calls.length === 0 ? '—' : stats.failed} change="Unable to connect" changeType={stats.failed > 0 ? 'negative' : 'neutral'} icon={AlertCircle} index={3} />
      </div>

      <div className="glass-panel overflow-hidden animate-fade-up stagger-2" style={{ animationFillMode: 'forwards' }}>
        <div className="p-5 sm:p-6 flex flex-col gap-4 border-b border-zinc-100/80">
          <div className="flex flex-col sm:flex-row gap-3">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-content-subtle" />
              <input
                type="search"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value)
                  setPage(0)
                }}
                placeholder="Search claims, patients, phone..."
                className="w-full pl-10 pr-4 py-2.5 text-sm text-content-primary border border-zinc-300 rounded-lg focus:border-brand-700 focus:ring-2 focus:ring-brand-700/20 outline-none"
                aria-label="Search calls"
              />
            </div>
            <select
              value={claimTypeFilter}
              onChange={(e) => {
                setClaimTypeFilter(e.target.value as ClaimTypeFilter)
                setPage(0)
              }}
              className="px-4 py-2.5 text-sm text-content-primary border border-zinc-300 rounded-lg bg-white focus:border-brand-700 outline-none sm:min-w-[200px]"
              aria-label="Filter by claim type"
            >
              {CLAIM_TYPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {loadError ? (
          <div className="p-10 text-center">
            <AlertCircle className="w-10 h-10 text-amber-500 mx-auto mb-3" />
            <p className="font-semibold text-content-primary">Call records could not load</p>
            <p className="text-sm text-content-muted mt-2 max-w-md mx-auto">{loadError}</p>
            <Button variant="secondary" size="sm" className="mt-4" onClick={load} icon={<RefreshCw className="w-4 h-4" />}>
              Retry
            </Button>
          </div>
        ) : loading && calls.length === 0 ? (
          <TableSkeleton rows={6} />
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={<Phone className="w-8 h-8" />}
            title="No call records yet"
            description="Follow up on a claim to start building your call history and conversation notes."
          />
        ) : (
          <>
            <div className="p-4 sm:p-5 space-y-3">
              {paginated.map((call) => {
                const isOpen = expanded === call.call_id
                const confidence = call.confidence_score ?? (call.lead_score != null ? call.lead_score / 100 : 0)
                const style = cardStyle(call.status)

                return (
                  <article
                    key={call.call_id}
                    className={`rounded-xl border border-zinc-200/90 border-l-4 ${style.border} ${style.bg} shadow-sm overflow-hidden transition-shadow hover:shadow-md`}
                  >
                    <button
                      type="button"
                      className="w-full text-left p-4 sm:p-5 flex flex-col sm:flex-row sm:items-center gap-4"
                      onClick={() => setExpanded(isOpen ? null : call.call_id)}
                    >
                      <div className="flex items-center gap-3 flex-1 min-w-0">
                        <div className={`w-11 h-11 rounded-xl border flex items-center justify-center shrink-0 ${style.iconBg}`}>
                          <Phone className={`w-5 h-5 ${style.icon}`} />
                        </div>
                        <div className="min-w-0">
                          <p className="font-semibold text-content-primary truncate">
                            {call.patient_name || call.customer_name || 'Patient'}
                            {call.claim_id ? (
                              <span className="text-content-muted font-normal"> · #{call.claim_id}</span>
                            ) : call.claim_type ? (
                              <span className="text-content-muted font-normal"> · {call.claim_type}</span>
                            ) : null}
                          </p>
                          <p className="text-xs text-content-muted truncate mt-0.5">
                            {call.payer_name || call.tpa_name || 'Insurance'} · {call.hospital_name || 'Hospital'}
                          </p>
                          <p className="text-[11px] text-content-subtle mt-1">{formatCallDate(call.created_at)}</p>
                        </div>
                      </div>

                      <div className="flex flex-wrap items-center gap-3 sm:justify-end">
                        <StatusBadge status={call.status} />
                        <span className="inline-flex items-center gap-1 text-sm text-content-secondary bg-white/70 border border-zinc-200 rounded-lg px-2.5 py-1">
                          <Star className="w-3.5 h-3.5 text-amber-500" />
                          {confidence.toFixed(2)}
                        </span>
                        {call.duration_seconds > 0 && (
                          <span className="inline-flex items-center gap-1 text-sm text-content-muted bg-white/70 border border-zinc-200 rounded-lg px-2.5 py-1">
                            <Clock className="w-3.5 h-3.5" />
                            {Math.round(call.duration_seconds)}s
                          </span>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation()
                            downloadCallJson(call.call_id).catch(console.error)
                          }}
                          icon={<Download className="w-3.5 h-3.5" />}
                        >
                          Export
                        </Button>
                        {isOpen ? <ChevronUp className="w-4 h-4 text-content-muted" /> : <ChevronDown className="w-4 h-4 text-content-muted" />}
                      </div>
                    </button>

                    {isOpen && (
                      <div className="px-4 sm:px-5 pb-5 pt-0 border-t border-zinc-200/70 bg-white/60 animate-fade-in">
                        {(call.resolution_code || call.denial_code || call.follow_up_date || call.resolution_status) && (
                          <div className="mb-4 grid sm:grid-cols-2 lg:grid-cols-4 gap-2 pt-4">
                            {call.resolution_status && (
                              <div className="text-xs bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2 text-emerald-900">
                                <span className="font-semibold">Claim status: </span>
                                {call.resolution_status}
                              </div>
                            )}
                            {call.resolution_code && (
                              <div className="text-xs bg-blue-50 border border-blue-200 rounded-lg px-3 py-2 text-blue-900">
                                <span className="font-semibold">Outcome: </span>
                                {call.resolution_code}
                              </div>
                            )}
                            {call.denial_code && (
                              <div className="text-xs bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-red-900">
                                <span className="font-semibold">Denial reason: </span>
                                {call.denial_code}
                              </div>
                            )}
                            {call.follow_up_date && (
                              <div className="text-xs bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-amber-900">
                                <span className="font-semibold">Follow-up: </span>
                                {call.follow_up_date}
                              </div>
                            )}
                          </div>
                        )}
                        <TurnLatencyBreakdown call={call} />
                        <CardHeader
                          title="Call notes"
                          description="Full conversation with the insurance company"
                          icon={<MessageSquare className="w-5 h-5" />}
                        />
                        {call.transcript.length > 0 ? (
                          <div className="space-y-2 max-h-80 overflow-y-auto scrollbar-thin pr-1">
                            {call.transcript.map((t, i) => (
                              <div
                                key={i}
                                className={`flex ${
                                  t.role === 'system'
                                    ? 'justify-center'
                                    : t.role === 'agent'
                                      ? 'justify-start'
                                      : 'justify-end'
                                }`}
                              >
                                <div
                                  className={`max-w-[85%] rounded-xl px-3.5 py-2 text-sm ${
                                    t.role === 'system'
                                      ? 'bg-amber-50 border border-amber-100 text-amber-900 text-xs'
                                      : t.role === 'agent'
                                        ? 'bg-white border border-zinc-200 text-content-secondary'
                                        : 'bg-brand-800 text-white'
                                  }`}
                                >
                                  <p className="text-[10px] opacity-70 mb-0.5">
                                    {t.role === 'agent' ? 'Your team' : t.role === 'customer' ? 'Insurance representative' : 'System note'}
                                  </p>
                                  {t.text}
                                  {t.metrics && (t.metrics.handler_ms != null || t.metrics.gap_since_last_agent_ms != null) && (
                                    <p className="text-[10px] opacity-60 mt-1.5 font-mono leading-relaxed">
                                      {t.metrics.gap_since_last_agent_ms != null && (
                                        <span>gap {Math.round(t.metrics.gap_since_last_agent_ms)}ms · </span>
                                      )}
                                      {t.metrics.handler_ms != null && (
                                        <span>handler {Math.round(t.metrics.handler_ms)}ms</span>
                                      )}
                                      {t.metrics.llm_ms != null && t.metrics.llm_ms > 0 && (
                                        <span> · llm {Math.round(t.metrics.llm_ms)}ms</span>
                                      )}
                                      {t.metrics.tts_ms != null && t.metrics.tts_ms > 0 && (
                                        <span> · tts {Math.round(t.metrics.tts_ms)}ms</span>
                                      )}
                                      {t.metrics.tts_cache_hit && <span> · tts cached</span>}
                                      {t.metrics.path && <span> · {t.metrics.path}</span>}
                                    </p>
                                  )}
                                </div>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <p className="text-sm text-content-muted">Call notes will appear here once the conversation begins.</p>
                        )}
                      </div>
                    )}
                  </article>
                )
              })}
            </div>

            {totalPages > 1 && (
              <div className="flex items-center justify-between px-5 py-4 border-t border-zinc-200">
                <p className="text-xs text-content-muted">
                  Showing {page * pageSize + 1}–{Math.min((page + 1) * pageSize, filtered.length)} of {filtered.length}
                </p>
                <div className="flex gap-2">
                  <Button variant="secondary" size="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
                    Previous
                  </Button>
                  <Button variant="secondary" size="sm" disabled={page >= totalPages - 1} onClick={() => setPage(page + 1)}>
                    Next
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
