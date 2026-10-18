import { useMemo, useState } from 'react'
import {
  Sparkles,
  Users,
  Search,
  ChevronDown,
  Check,
  HeartHandshake,
  Flame,
  Scale,
  GraduationCap,
  type LucideIcon,
} from 'lucide-react'
import { TPA_PROFILES } from '../api'
import Badge from './ui/Badge'

interface Props {
  tpaProfile: string
  disabled?: boolean
  onProfileChange: (profile: string) => void
}

type CategoryId = 'collaborative' | 'challenging' | 'process' | 'experience'

interface ProfileMeta {
  hint: string
  category: CategoryId
}

interface CategoryMeta {
  id: CategoryId
  label: string
  subtitle: string
  icon: LucideIcon
  accent: string
  row: string
  rowExpanded: string
  iconBox: string
  panel: string
  chip: string
  selectedCard: string
  profileIdle: string
  profileActive: string
}

const surface = {
  accent: 'text-slate-800',
  row: 'border-slate-200 bg-white',
  rowExpanded: 'border-brand-800 shadow-sm ring-1 ring-brand-800/15',
  iconBox: 'bg-slate-100 ring-1 ring-slate-200',
  panel: 'bg-slate-50 border-t border-slate-200',
  chip: 'bg-slate-100 text-slate-700 border-slate-200',
  selectedCard: 'bg-slate-50 border-brand-800',
  profileIdle: 'bg-white border-slate-200 hover:border-slate-400',
  profileActive: 'bg-white border-brand-800 shadow-sm ring-1 ring-brand-800',
}

const CATEGORIES: CategoryMeta[] = [
  { id: 'collaborative', label: 'Collaborative', subtitle: 'Warm, helpful reps', icon: HeartHandshake, ...surface },
  { id: 'challenging', label: 'Challenging', subtitle: 'Pushback & friction', icon: Flame, ...surface },
  { id: 'process', label: 'Process & rules', subtitle: 'Policy-driven reps', icon: Scale, ...surface },
  { id: 'experience', label: 'Experience', subtitle: 'Pace & familiarity', icon: GraduationCap, ...surface },
]

const PROFILE_META: Record<string, ProfileMeta> = {
  'Helpful Executive': { category: 'collaborative', hint: 'Shares status openly, willing to help' },
  'Cooperative Executive': { category: 'collaborative', hint: 'Works with you to resolve the claim' },
  'Empathetic Executive': { category: 'collaborative', hint: 'Patient listener, understands hospital urgency' },
  'Polite Executive': { category: 'collaborative', hint: 'Courteous tone, thorough but calm' },
  'Knowledgeable Executive': { category: 'collaborative', hint: 'Knows the file and policy details well' },
  'Busy Executive': { category: 'challenging', hint: 'Rushed — keep questions short and direct' },
  'Uncooperative Executive': { category: 'challenging', hint: 'Minimal answers, needs firm persistence' },
  'Impatient Executive': { category: 'challenging', hint: 'Wants speed — avoid long explanations' },
  'Defensive Executive': { category: 'challenging', hint: 'Guards decisions, respond with evidence' },
  'Confused Executive': { category: 'challenging', hint: 'Unsure of process — guide step by step' },
  'Process Driven Executive': { category: 'process', hint: 'Follows workflow strictly, no shortcuts' },
  'Rule Focused Executive': { category: 'process', hint: 'Cites policy clauses and compliance rules' },
  'Technical Executive': { category: 'process', hint: 'Focuses on codes, docs, and system details' },
  'Escalation Focused Executive': { category: 'process', hint: 'Quick to escalate — stay composed' },
  'Supervisor Type': { category: 'process', hint: 'Senior authority, expects clear facts' },
  'Experienced Executive': { category: 'experience', hint: 'Seasoned adjuster, expects professionalism' },
  'New Executive': { category: 'experience', hint: 'Learning the system — be patient and clear' },
  'Slow Response Executive': { category: 'experience', hint: 'Needs time to look things up' },
  'Fast Response Executive': { category: 'experience', hint: 'Rapid answers, match their pace' },
  'Incomplete Information Executive': { category: 'experience', hint: 'Partial info — probe with follow-ups' },
}

function profileLabel(profile: string): string {
  return profile.replace(' Executive', '')
}

function getCategoryForProfile(profile: string): CategoryMeta {
  const categoryId = PROFILE_META[profile]?.category ?? 'collaborative'
  return CATEGORIES.find((c) => c.id === categoryId) ?? CATEGORIES[0]
}

function profilesForCategory(categoryId: CategoryId): string[] {
  return TPA_PROFILES.filter((p) => PROFILE_META[p]?.category === categoryId)
}

export default function ScenarioSelectionPanel({ tpaProfile, disabled, onProfileChange }: Props) {
  const [expandedCategory, setExpandedCategory] = useState<CategoryId | null>(
    tpaProfile ? getCategoryForProfile(tpaProfile).id : null
  )
  const [search, setSearch] = useState('')

  const searchResults = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return []
    return TPA_PROFILES.filter(
      (p) =>
        profileLabel(p).toLowerCase().includes(q) ||
        PROFILE_META[p]?.hint.toLowerCase().includes(q) ||
        p.toLowerCase().includes(q)
    )
  }, [search])

  const toggleCategory = (categoryId: CategoryId) => {
    setExpandedCategory((current) => (current === categoryId ? null : categoryId))
  }

  const selectProfile = (profile: string) => {
    onProfileChange(tpaProfile === profile ? '' : profile)
    setExpandedCategory(null)
  }

  const selectedCategory = tpaProfile ? getCategoryForProfile(tpaProfile) : null
  const selectedHint = tpaProfile ? PROFILE_META[tpaProfile]?.hint : ''

  return (
    <section className="rounded-2xl bg-white ring-1 ring-slate-200 shadow-sm overflow-hidden">
      <div className="px-5 py-4 border-b border-slate-200 bg-slate-50">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <span className="w-7 h-7 rounded-lg bg-slate-500/20 ring-1 ring-slate-300/60 flex items-center justify-center">
                <Sparkles className="w-4 h-4 text-slate-700" />
              </span>
              Expected payer representative
            </h2>
            <p className="text-xs text-slate-600 mt-1 ml-9">
              Chris adapts tone and negotiation to the rep type you expect on this call
            </p>
          </div>
          <Badge variant="generating" dot>
            AI dialogue active
          </Badge>
        </div>
      </div>

      <div className="p-4 space-y-4 bg-gradient-to-b from-slate-50/50 to-white">
        {tpaProfile && selectedCategory && (
          <div className={`rounded-xl border p-4 shadow-sm ${selectedCategory.selectedCard}`}>
            <div className="flex items-start gap-3">
              <div className={`w-10 h-10 rounded-xl flex items-center justify-center shrink-0 ${selectedCategory.iconBox}`}>
                <selectedCategory.icon className={`w-5 h-5 ${selectedCategory.accent}`} />
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-[10px] font-bold uppercase tracking-wider text-slate-600">Selected rep type</p>
                <p className="text-base font-bold text-slate-900 mt-0.5">{profileLabel(tpaProfile)}</p>
                <p className="text-xs text-slate-700 mt-1 leading-relaxed">{selectedHint}</p>
              </div>
              <span className={`shrink-0 text-[10px] font-semibold px-2 py-1 rounded-full border ${selectedCategory.chip}`}>
                {selectedCategory.label}
              </span>
            </div>
            <p className="text-[11px] text-slate-800 font-medium mt-3 pt-3 border-t border-white/70">
              Chris will adapt pacing and negotiation for this rep style.
            </p>
          </div>
        )}

        <div>
          <div className="flex items-center justify-between gap-2 mb-3">
            <p className="text-[11px] font-bold text-brand-800 uppercase tracking-wide flex items-center gap-1.5">
              <Users className="w-3 h-3" /> Choose rep profile
            </p>
            <div className="relative w-full max-w-[200px]">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-brand-600" />
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search reps..."
                className="w-full pl-8 pr-3 py-1.5 text-xs rounded-lg border border-brand-200 bg-white focus:border-brand-400 focus:ring-2 focus:ring-brand-400/25 outline-none"
                aria-label="Search payer representative profiles"
              />
            </div>
          </div>

          {!search.trim() ? (
            <div className="space-y-2">
              {CATEGORIES.map((cat) => {
                const Icon = cat.icon
                const expanded = expandedCategory === cat.id
                const count = profilesForCategory(cat.id).length
                const hasSelection = profilesForCategory(cat.id).includes(tpaProfile)

                return (
                  <div
                    key={cat.id}
                    className={`rounded-xl border overflow-hidden transition-all ${expanded ? cat.rowExpanded : cat.row}`}
                  >
                    <button
                      type="button"
                      disabled={disabled}
                      onClick={() => toggleCategory(cat.id)}
                      className={`w-full flex items-center gap-3 p-3 text-left transition-colors hover:brightness-[1.02] ${
                        disabled ? 'opacity-60 cursor-not-allowed' : ''
                      }`}
                      aria-expanded={expanded}
                    >
                      <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${cat.iconBox}`}>
                        <Icon className={`w-4 h-4 ${cat.accent}`} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-bold text-slate-900">{cat.label}</p>
                        <p className="text-[10px] text-slate-600">
                          {cat.subtitle} · {count} types
                        </p>
                      </div>
                      {hasSelection && !expanded && (
                        <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border shrink-0 ${cat.chip}`}>
                          {profileLabel(tpaProfile)}
                        </span>
                      )}
                      <ChevronDown
                        className={`w-4 h-4 shrink-0 transition-transform duration-200 ${cat.accent} ${
                          expanded ? 'rotate-180' : ''
                        }`}
                      />
                    </button>

                    {expanded && (
                      <div className={`px-3 pb-3 pt-1 ${cat.panel}`}>
                        <div className="grid sm:grid-cols-2 gap-2">
                          {profilesForCategory(cat.id).map((p) => {
                            const active = tpaProfile === p
                            const meta = PROFILE_META[p]
                            return (
                              <button
                                key={p}
                                type="button"
                                disabled={disabled}
                                onClick={() => selectProfile(p)}
                                className={`group relative text-left rounded-xl p-3 border transition-all ${
                                  active ? cat.profileActive : cat.profileIdle
                                } ${disabled ? 'opacity-60 cursor-not-allowed' : ''}`}
                              >
                                {active && (
                                  <span className="absolute top-2.5 right-2.5 w-5 h-5 rounded-full bg-brand-600 text-white flex items-center justify-center">
                                    <Check className="w-3 h-3" strokeWidth={3} />
                                  </span>
                                )}
                                <p
                                  className={`text-sm font-bold pr-6 ${
                                    active ? 'text-slate-900' : 'text-slate-800 group-hover:text-slate-900'
                                  }`}
                                >
                                  {profileLabel(p)}
                                </p>
                                <p className="text-[10px] text-slate-600 mt-1 leading-snug line-clamp-2">
                                  {meta?.hint}
                                </p>
                              </button>
                            )
                          })}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="grid sm:grid-cols-2 gap-2">
              {searchResults.map((p) => {
                const active = tpaProfile === p
                const meta = PROFILE_META[p]
                const cat = getCategoryForProfile(p)
                return (
                  <button
                    key={p}
                    type="button"
                    disabled={disabled}
                    onClick={() => selectProfile(p)}
                    className={`group relative text-left rounded-xl p-3 border transition-all ${
                      active ? cat.profileActive : cat.profileIdle
                    } ${disabled ? 'opacity-60 cursor-not-allowed' : ''}`}
                  >
                    {active && (
                      <span className="absolute top-2.5 right-2.5 w-5 h-5 rounded-full bg-brand-600 text-white flex items-center justify-center">
                        <Check className="w-3 h-3" strokeWidth={3} />
                      </span>
                    )}
                    <p
                      className={`text-sm font-bold pr-6 ${
                        active ? 'text-slate-900' : 'text-slate-800 group-hover:text-slate-900'
                      }`}
                    >
                      {profileLabel(p)}
                    </p>
                    <p className="text-[10px] text-slate-600 mt-1 leading-snug line-clamp-2">{meta?.hint}</p>
                    <span className={`inline-block mt-2 text-[9px] font-semibold px-1.5 py-0.5 rounded border ${cat.chip}`}>
                      {cat.label}
                    </span>
                  </button>
                )
              })}
              {searchResults.length === 0 && (
                <p className="text-xs text-slate-500 text-center py-6 sm:col-span-2">No rep profiles match your search.</p>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
