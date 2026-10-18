import { useEffect, useState } from 'react'
import { RefreshCw, ChevronDown, ChevronUp, ShieldCheck } from 'lucide-react'
import { fetchScenarioDuplicates, type CallRecord } from '../api'
import { findLiveDuplicateRisks } from '../utils/duplicateCalls'
import Button from './ui/Button'

interface Props {
  calls: CallRecord[]
  loading?: boolean
  onRefresh?: () => void
}

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      className={`w-2 h-2 rounded-full shrink-0 ${ok ? 'bg-emerald-500' : 'bg-amber-500'}`}
      aria-hidden
    />
  )
}

export default function DuplicateDetectionPanel({ calls, loading, onRefresh }: Props) {
  const [audit, setAudit] = useState<Awaited<ReturnType<typeof fetchScenarioDuplicates>> | null>(null)
  const [auditLoading, setAuditLoading] = useState(true)
  const [auditError, setAuditError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)

  const liveRisks = findLiveDuplicateRisks(calls)
  const liveOk = liveRisks.length === 0
  const corpusOk = audit?.status === 'pass'
  const allOk = liveOk && corpusOk && !auditError && !auditLoading && !loading
  const hasIssues = !liveOk || (audit && !corpusOk) || auditError

  const loadAudit = () => {
    setAuditLoading(true)
    setAuditError(null)
    fetchScenarioDuplicates()
      .then(setAudit)
      .catch((err) => setAuditError(err instanceof Error ? err.message : 'Audit unavailable'))
      .finally(() => setAuditLoading(false))
  }

  useEffect(() => {
    loadAudit()
  }, [])

  const refreshAll = () => {
    loadAudit()
    onRefresh?.()
  }

  const busy = loading || auditLoading

  return (
    <div className="rounded-xl bg-white border border-slate-200/80 shadow-sm overflow-hidden border-l-4 border-l-brand-500">
      <div className="px-4 py-3.5 flex items-center justify-between gap-3 border-b border-slate-200 bg-slate-50">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-9 h-9 rounded-lg bg-brand-600 flex items-center justify-center shrink-0 shadow-sm">
            <ShieldCheck className="w-4 h-4 text-white" strokeWidth={2} />
          </div>
          <div className="min-w-0">
            <h2 className="text-base font-bold text-slate-900 tracking-tight">Duplicate checks</h2>
            <p className="text-[11px] text-slate-500 mt-0.5">Live call guard · training example audit</p>
          </div>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={refreshAll}
          disabled={busy}
          icon={<RefreshCw className={`w-3.5 h-3.5 ${busy ? 'animate-spin' : ''}`} />}
          aria-label="Refresh duplicate checks"
        />
      </div>

      <div className="p-4 grid sm:grid-cols-2 gap-3">
        {/* Live calls */}
        <div className="rounded-lg border border-slate-200/80 px-3.5 py-3 bg-slate-50/50">
          <div className="flex items-center justify-between gap-2 mb-1">
            <p className="text-xs font-semibold text-slate-800">Live calls</p>
            {!loading && (
              <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-slate-600">
                <StatusDot ok={liveOk} />
                {liveOk ? 'Clear' : `${liveRisks.length} blocked`}
              </span>
            )}
          </div>
          <p className="text-[11px] text-slate-500 leading-snug">
            {loading
              ? 'Checking records…'
              : liveOk
                ? 'No active or recent repeat dials'
                : 'Same claim + phone cannot be redialed yet'}
          </p>
        </div>

        {/* Training audit */}
        <div className="rounded-lg border border-slate-200/80 px-3.5 py-3 bg-slate-50/50">
          <div className="flex items-center justify-between gap-2 mb-1">
            <p className="text-xs font-semibold text-slate-800">Training data</p>
            {auditLoading ? (
              <span className="text-[11px] text-slate-400">…</span>
            ) : auditError ? (
              <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-amber-700">
                <StatusDot ok={false} />
                Offline
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-slate-600">
                <StatusDot ok={!!corpusOk} />
                {corpusOk ? 'Pass' : 'Review'}
              </span>
            )}
          </div>
          <p className="text-[11px] text-slate-500 leading-snug">
            {auditLoading
              ? 'Running audit…'
              : auditError
                ? 'Backend required for corpus check'
                : audit
                  ? `${audit.total_scenarios} examples · ${audit.flagged_count} flagged · ${audit.duplicate_percentage}% overlap`
                  : '—'}
          </p>
        </div>
      </div>

      {allOk && (
        <p className="px-4 pb-3 text-[11px] text-emerald-700 font-medium">All clear — no duplicate risks detected.</p>
      )}

      {hasIssues && (
        <div className="border-t border-slate-100">
          <button
            type="button"
            onClick={() => setExpanded(!expanded)}
            className="w-full px-4 py-2 flex items-center justify-between text-[11px] font-semibold text-brand-700 hover:bg-slate-50 transition-colors"
          >
            {expanded ? 'Hide details' : 'View details'}
            {expanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
          </button>

          {expanded && (
            <div className="px-4 pb-4 space-y-3 text-[11px] text-slate-600">
              {!liveOk && (
                <ul className="space-y-1.5">
                  {liveRisks.map((r) => (
                    <li key={r.call_id} className="rounded-md bg-amber-50 border border-amber-100 px-2.5 py-2">
                      <span className="font-semibold text-slate-800">
                        {r.patient_name || 'Patient'} · #{r.claim_id}
                      </span>
                      <span className="text-slate-500"> — {r.reason === 'active_call' ? 'On call' : 'Completed <24h'}</span>
                    </li>
                  ))}
                </ul>
              )}

              {auditError && <p className="text-amber-800">{auditError}</p>}

              {audit && !corpusOk && (
                <div className="space-y-2">
                  <p className="text-orange-800 font-medium">{audit.message}</p>
                  {audit.near_duplicate_pairs.slice(0, 6).map((p) => (
                    <p key={`${p.scenario_a}-${p.scenario_b}`} className="rounded-md bg-slate-50 border border-slate-100 px-2.5 py-1.5">
                      {p.scenario_a} ↔ {p.scenario_b}
                      <span className="text-slate-700 ml-1">({(p.similarity * 100).toFixed(0)}%)</span>
                    </p>
                  ))}
                  {audit.near_duplicate_pairs.length > 6 && (
                    <p className="text-slate-400">+{audit.near_duplicate_pairs.length - 6} more pairs</p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
