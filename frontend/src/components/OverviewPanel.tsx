import { useEffect, useMemo, useState } from 'react'
import { Phone, FileCheck, Clock, ArrowRight, RefreshCw, Activity, Layers, ChevronDown, History } from 'lucide-react'
import { fetchCalls, type CallRecord } from '../api'
import Badge from './ui/Badge'
import Button from './ui/Button'
import KpiCard from './ui/KpiCard'
import DuplicateDetectionPanel from './DuplicateDetectionPanel'
import { TableSkeleton } from './ui/EmptyState'

interface Props {
  onNavigateLaunch: () => void
}

function isToday(ts: number) {
  return new Date(ts * 1000).toDateString() === new Date().toDateString()
}

function friendlyStatus(status: string) {
  const normalized = status.replace(/_/g, '-')
  const map: Record<string, string> = {
    initiated: 'Started',
    calling: 'Calling',
    ringing: 'Ringing',
    answered: 'Connected',
    'in-progress': 'In progress',
    completed: 'Completed',
    failed: 'Failed',
  }
  return map[normalized] || status.replace(/-/g, ' ')
}

function formatWhen(ts: number) {
  const d = new Date(ts * 1000)
  const now = new Date()
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

function formatDuration(seconds: number) {
  if (!seconds) return '—'
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

export default function OverviewPanel({ onNavigateLaunch }: Props) {
  const [calls, setCalls] = useState<CallRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [recentExpanded, setRecentExpanded] = useState(true)

  const load = () => {
    setLoading(true)
    fetchCalls()
      .then((c) => setCalls(c.sort((a, b) => b.created_at - a.created_at)))
      .catch(console.error)
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
  }, [])

  const stats = useMemo(() => {
    const today = calls.filter((c) => isToday(c.created_at))
    const completed = calls.filter((c) => c.status === 'completed')
    const active = calls.filter((c) =>
      ['initiated', 'calling', 'ringing', 'in-progress', 'answered'].includes(c.status)
    )
    return { today: today.length, completed: completed.length, active: active.length, total: calls.length }
  }, [calls])

  return (
    <div className="max-w-[1280px] mx-auto space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">Dashboard</h1>
          <p className="text-sm text-slate-500 mt-0.5">Today's follow-up activity and recent call outcomes</p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" onClick={load} disabled={loading} icon={<RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />}>
            Refresh
          </Button>
          <Button onClick={onNavigateLaunch} size="sm" className="font-semibold" icon={<Phone className="w-3.5 h-3.5" />}>
            Follow up on a claim
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          label="Today"
          value={loading ? '—' : stats.today}
          change="Calls placed"
          icon={Phone}
          index={0}
        />
        <KpiCard
          label="In progress"
          value={loading ? '—' : stats.active}
          change="Active now"
          icon={Activity}
          index={1}
        />
        <KpiCard
          label="Completed"
          value={loading ? '—' : stats.completed}
          change="Documented"
          icon={FileCheck}
          index={2}
        />
        <KpiCard
          label="All time"
          value={loading ? '—' : stats.total}
          change="Total records"
          icon={Layers}
          index={3}
        />
      </div>

      <DuplicateDetectionPanel calls={calls} loading={loading} onRefresh={load} />

      <div className="rounded-xl bg-white border border-slate-200/80 shadow-sm overflow-hidden border-l-4 border-l-slate-500">
        <button
          type="button"
          onClick={() => setRecentExpanded((v) => !v)}
          className="w-full px-4 py-3.5 flex items-center justify-between gap-3 border-b border-slate-100/80 bg-gradient-to-r from-slate-50/70 to-white text-left hover:from-slate-50 transition-colors"
          aria-expanded={recentExpanded}
        >
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-9 h-9 rounded-lg bg-slate-600 flex items-center justify-center shrink-0 shadow-sm">
              <History className="w-4 h-4 text-white" strokeWidth={2} />
            </div>
            <div className="min-w-0">
              <h2 className="text-base font-bold text-slate-900 tracking-tight">Recent follow-up calls</h2>
              <p className="text-[11px] text-slate-500 mt-0.5">
                Latest payer conversations
                {calls.length > 0 && ` · ${calls.length} total`}
              </p>
            </div>
          </div>
          <ChevronDown
            className={`w-5 h-5 text-slate-600 shrink-0 transition-transform duration-200 ${recentExpanded ? 'rotate-180' : ''}`}
          />
        </button>

        {recentExpanded && (
          <>
        {loading ? (
          <TableSkeleton rows={5} />
        ) : calls.length === 0 ? (
          <div className="py-14 text-center px-6">
            <div className="w-12 h-12 rounded-xl bg-brand-50 flex items-center justify-center mx-auto mb-3 ring-1 ring-brand-100">
              <Phone className="w-5 h-5 text-brand-700" />
            </div>
            <p className="text-sm font-semibold text-slate-900">No calls yet</p>
            <p className="text-xs text-slate-500 mt-1 mb-5">Place your first insurance follow-up from the Follow Up tab.</p>
            <Button onClick={onNavigateLaunch} size="sm" icon={<ArrowRight className="w-3.5 h-3.5" />}>
              Go to Follow Up
            </Button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/50">
                  <th className="text-left px-5 py-2.5 text-[11px] font-bold text-slate-500 uppercase tracking-wide">Patient / Claim</th>
                  <th className="text-left px-5 py-2.5 text-[11px] font-bold text-slate-500 uppercase tracking-wide hidden md:table-cell">Payer</th>
                  <th className="text-left px-5 py-2.5 text-[11px] font-bold text-slate-500 uppercase tracking-wide hidden sm:table-cell">When</th>
                  <th className="text-left px-5 py-2.5 text-[11px] font-bold text-slate-500 uppercase tracking-wide hidden lg:table-cell">Duration</th>
                  <th className="text-left px-5 py-2.5 text-[11px] font-bold text-slate-500 uppercase tracking-wide">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {calls.slice(0, 10).map((c) => (
                  <tr key={c.call_id} className="hover:bg-slate-50/80 transition-colors">
                    <td className="px-5 py-3">
                      <p className="font-semibold text-slate-900">{c.patient_name || c.customer_name || 'Patient'}</p>
                      <p className="text-xs text-slate-500 mt-0.5">
                        {c.claim_id ? `#${c.claim_id}` : 'No claim #'} · {c.claim_type || 'Claim'}
                      </p>
                    </td>
                    <td className="px-5 py-3 text-slate-600 hidden md:table-cell">
                      {c.payer_name || c.tpa_name || 'Insurance'}
                    </td>
                    <td className="px-5 py-3 text-slate-500 text-xs hidden sm:table-cell tabular-nums">
                      {formatWhen(c.created_at)}
                    </td>
                    <td className="px-5 py-3 text-slate-500 text-xs hidden lg:table-cell tabular-nums">
                      <span className="inline-flex items-center gap-1">
                        <Clock className="w-3 h-3" />
                        {formatDuration(c.duration_seconds)}
                      </span>
                    </td>
                    <td className="px-5 py-3">
                      <Badge status={c.status.replace(/_/g, '-')} dot>{friendlyStatus(c.status)}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {calls.length > 0 && (
          <div className="px-5 py-3 border-t border-slate-100 bg-slate-50/40 flex items-center justify-between">
            <p className="text-xs text-slate-500 flex items-center gap-1.5">
              <FileCheck className="w-3.5 h-3.5 text-brand-600" />
              Outcomes and full transcripts are in Call Records
            </p>
            <button
              type="button"
              onClick={onNavigateLaunch}
              className="text-xs font-semibold text-brand-700 hover:text-brand-800"
            >
              New follow-up →
            </button>
          </div>
        )}
          </>
        )}
      </div>
    </div>
  )
}
