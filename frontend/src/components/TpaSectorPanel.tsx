import { useEffect, useMemo, useState } from 'react'
import { Search, Check, Layers, ChevronRight } from 'lucide-react'
import { fetchTpaSectors, type TpaSector } from '../api'

interface Props {
  selectedSectorId: string
  focusTopics: string[]
  disabled?: boolean
  onSectorChange: (sectorId: string) => void
  onFocusTopicsChange: (topics: string[]) => void
}

export default function TpaSectorPanel({
  selectedSectorId,
  focusTopics,
  disabled,
  onSectorChange,
  onFocusTopicsChange,
}: Props) {
  const [sectors, setSectors] = useState<TpaSector[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')

  useEffect(() => {
    let alive = true
    fetchTpaSectors()
      .then((res) => {
        if (!alive) return
        setSectors(res.sectors || [])
        if (!selectedSectorId && res.sectors?.[0]?.id) {
          onSectorChange(res.sectors[0].id)
        }
      })
      .catch((err) => {
        if (!alive) return
        setError(err instanceof Error ? err.message : 'Failed to load TPA sectors')
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selected = useMemo(
    () => sectors.find((s) => s.id === selectedSectorId) || null,
    [sectors, selectedSectorId],
  )

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return sectors
    return sectors.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        s.short_name.toLowerCase().includes(q) ||
        s.topics.some((t) => t.toLowerCase().includes(q)) ||
        String(s.number).includes(q),
    )
  }, [sectors, search])

  const toggleTopic = (topic: string) => {
    if (disabled) return
    if (focusTopics.includes(topic)) {
      onFocusTopicsChange(focusTopics.filter((t) => t !== topic))
    } else {
      onFocusTopicsChange([...focusTopics, topic])
    }
  }

  if (loading) {
    return (
      <div className="rounded-2xl bg-white ring-1 ring-slate-200 px-5 py-6 text-sm text-slate-500">
        Loading TPA service sectors…
      </div>
    )
  }

  if (error) {
    return (
      <div className="rounded-2xl bg-red-50 ring-1 ring-red-200 px-5 py-4 text-sm text-red-800">
        {error}
      </div>
    )
  }

  return (
    <div className="rounded-2xl bg-white ring-1 ring-slate-200/80 shadow-[0_4px_24px_rgba(15,23,42,0.06)] overflow-hidden">
      <div className="px-5 py-4 border-b border-slate-200 bg-slate-50">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <Layers className="w-4 h-4 text-brand-700" />
              TPA service sector
            </h2>
            <p className="text-[11px] text-slate-500 mt-0.5">
              Choose one of 25 areas. Focus topics become dynamic questions for the call.
            </p>
          </div>
          {selected && (
            <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-lg bg-brand-600 text-xs font-semibold text-white">
              {selected.number}. {selected.short_name}
            </span>
          )}
        </div>
        <div className="mt-3 relative max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            disabled={disabled}
            placeholder="Search sectors or topics…"
            className="w-full pl-9 pr-3 py-2 rounded-lg border border-slate-200 text-sm focus:outline-none focus:ring-2 focus:ring-sky-400/40"
          />
        </div>
      </div>

      <div className="grid lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] divide-y lg:divide-y-0 lg:divide-x divide-slate-100">
        <div className="max-h-[340px] overflow-y-auto p-3 grid sm:grid-cols-2 gap-2">
          {filtered.map((sector) => {
            const active = sector.id === selectedSectorId
            return (
              <button
                key={sector.id}
                type="button"
                disabled={disabled}
                onClick={() => {
                  onSectorChange(sector.id)
                  onFocusTopicsChange([])
                }}
                className={`text-left rounded-xl border px-3 py-2.5 transition ${
                  active
                    ? 'border-sky-400 bg-brand-50 ring-2 ring-sky-300/50'
                    : 'border-slate-200 bg-white hover:border-sky-300 hover:bg-slate-50'
                } disabled:opacity-60`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="text-[11px] font-semibold text-slate-400">#{sector.number}</p>
                    <p className="text-sm font-bold text-slate-900 leading-snug">{sector.short_name}</p>
                    <p className="text-[11px] text-slate-500 mt-0.5 line-clamp-2">{sector.description}</p>
                  </div>
                  {active ? (
                    <Check className="w-4 h-4 text-brand-700 shrink-0" />
                  ) : (
                    <ChevronRight className="w-4 h-4 text-slate-300 shrink-0" />
                  )}
                </div>
              </button>
            )
          })}
        </div>

        <div className="p-4 bg-slate-50/40 min-h-[280px]">
          {selected ? (
            <>
              <h3 className="text-sm font-bold text-slate-900">{selected.name}</h3>
              <p className="text-xs text-slate-600 mt-1">{selected.description}</p>

              <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wide mt-4 mb-2">
                Focus topics (optional — multi-select)
              </p>
              <div className="flex flex-wrap gap-1.5 max-h-[140px] overflow-y-auto pr-1">
                {selected.topics.map((topic) => {
                  const on = focusTopics.includes(topic)
                  return (
                    <button
                      key={topic}
                      type="button"
                      disabled={disabled}
                      onClick={() => toggleTopic(topic)}
                      className={`text-[11px] px-2.5 py-1 rounded-full border transition ${
                        on
                          ? 'bg-brand-600 border-sky-600 text-white'
                          : 'bg-white border-slate-200 text-slate-700 hover:border-sky-300'
                      } disabled:opacity-60`}
                    >
                      {topic}
                    </button>
                  )
                })}
              </div>

              <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wide mt-4 mb-2">
                Example questions (patterns only — AI invents live wording)
              </p>
              <ul className="space-y-1.5">
                {selected.sample_questions.map((q) => (
                  <li key={q} className="text-xs text-slate-700 pl-3 relative before:content-['•'] before:absolute before:left-0 before:text-brand-600">
                    {q}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="text-sm text-slate-500">Select a sector to see topics and sample questions.</p>
          )}
        </div>
      </div>
    </div>
  )
}
