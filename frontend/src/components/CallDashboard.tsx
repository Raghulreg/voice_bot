import { useEffect, useRef, useState } from 'react'
import {
  Phone,
  AlertCircle,
  CheckCircle2,
  Building2,
  FileText,
  Calendar,
  User,
  ChevronDown,
  ChevronUp,
  Users,
} from 'lucide-react'
import {
  initiateCall,
  connectPublicUrl,
  fetchCall,
  DEFAULT_LANGUAGE,
  CLAIM_TYPES,
  type HealthStatus,
  type CallRecord,
} from '../api'
import { callLaunchBlockers, isCallLaunchReady } from '../utils/callReady'
import { validatePhoneForCall, normalizePhoneInput, suggestIndianNumberFix } from '../utils/phone'
import { computeClaimProgressStep, isCallTerminal } from '../utils/callProgress'
import { Field } from './ui/Input'
import Button from './ui/Button'
import CallExperiencePanel from './CallExperiencePanel'
import { unlockMicrophone } from '../utils/micUnlock'
import ScenarioSelectionPanel from './ScenarioSelectionPanel'
import TpaSectorPanel from './TpaSectorPanel'
import { HorizontalClaimProgress } from './WorkflowTimeline'

interface Props {
  health: HealthStatus | null
  onHealthRefresh: () => void
}

export default function CallDashboard({ health, onHealthRefresh }: Props) {
  const [phone, setPhone] = useState('')
  const [hospitalName, setHospitalName] = useState('Apollo Hospital Bengaluru')
  const [claimId, setClaimId] = useState('')
  const [patientName, setPatientName] = useState('')
  const [claimType, setClaimType] = useState('Claim Pending')
  const [payerName, setPayerName] = useState('')
  const [npi, setNpi] = useState('')
  const [policyNumber, setPolicyNumber] = useState('')
  const [claimAgeDays, setClaimAgeDays] = useState('35')
  const [agentName, setAgentName] = useState('Chris')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<{ type: 'success' | 'error'; message: string } | null>(null)
  const [activeCallId, setActiveCallId] = useState<string | null>(null)
  const [activeCall, setActiveCall] = useState<CallRecord | null>(null)
  const [callStartedAt, setCallStartedAt] = useState<number | null>(null)
  const [urlMessage, setUrlMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [phoneHint, setPhoneHint] = useState<string | null>(null)
  const [showExtra, setShowExtra] = useState(false)
  const [tpaProfile, setTpaProfile] = useState('Helpful Executive')
  const [tpaSectorId, setTpaSectorId] = useState('claims')
  const [focusTopics, setFocusTopics] = useState<string[]>([])
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer = 0
    const attempt = (tryNo: number) => {
      setUrlMessage({ type: 'success', text: 'Connecting the public link…' })
      void connectPublicUrl()
        .then(() => {
          if (cancelled) return
          setUrlMessage(null)
          onHealthRefresh()
        })
        .catch((err: unknown) => {
          if (cancelled) return
          const message = err instanceof Error ? err.message : 'Could not connect the public link'
          const stillStarting = /public link|not reachable|failed to fetch|network|timed out|abort|call service/i.test(message)
          if (stillStarting) {
            timer = window.setTimeout(() => attempt(tryNo + 1), 1500)
            return
          }
          setUrlMessage({ type: 'error', text: message })
        })
    }
    attempt(1)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [onHealthRefresh])

  const refreshActiveCall = async (callId: string) => {
    try {
      const record = await fetchCall(callId)
      setActiveCall(record)
      return record
    } catch {
      return null
    }
  }

  useEffect(() => {
    if (!activeCallId) return

    void refreshActiveCall(activeCallId)

    pollRef.current = setInterval(async () => {
      const record = await refreshActiveCall(activeCallId)
      if (record && isCallTerminal(record.status)) {
        if (pollRef.current) {
          clearInterval(pollRef.current)
          pollRef.current = null
        }
      }
    }, 800)

    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [activeCallId])

  const handleCall = async () => {
    if (!tpaSectorId) {
      setResult({ type: 'error', message: 'Select a TPA service sector before calling.' })
      return
    }
    if (!tpaProfile) {
      setResult({ type: 'error', message: 'Select an expected payer representative type before calling.' })
      return
    }

    const cleaned = normalizePhoneInput(phone)
    const autoFix = suggestIndianNumberFix(cleaned)
    const dialNumber = autoFix || cleaned

    const check = validatePhoneForCall(autoFix ? dialNumber : phone)
    if (!check.ok && !autoFix) {
      setResult({ type: 'error', message: check.message || 'Please enter a valid phone number' })
      if (check.suggestedFix) setPhoneHint(check.suggestedFix)
      return
    }

    if (autoFix) {
      setPhone(autoFix)
      setPhoneHint(`Updated to ${autoFix}`)
    }

    unlockMicrophone()
    setLoading(true)
    setResult(null)
    setActiveCall(null)
    const ageParsed = claimAgeDays.trim() ? parseInt(claimAgeDays, 10) : undefined
    try {
      const res = await initiateCall({
        phone_number: dialNumber,
        patient_name: patientName,
        agent_name: agentName,
        preferred_language: DEFAULT_LANGUAGE,
        hospital_name: hospitalName,
        claim_id: claimId,
        claim_type: claimType,
        payer_name: payerName,
        tpa_name: payerName,
        npi,
        policy_number: policyNumber,
        claim_age_days: Number.isFinite(ageParsed) ? ageParsed : undefined,
        tpa_executive_profile: tpaProfile,
        tpa_sector_id: tpaSectorId,
        focus_topics: focusTopics,
      })
      setActiveCallId(res.call_id)
      setCallStartedAt(Date.now())
      setResult({
        type: 'success',
        message: `Call started for ${tpaSectorId.replace(/_/g, ' ')} — live transcript updates below.`,
      })
      void refreshActiveCall(res.call_id)
    } catch (err) {
      setActiveCallId(null)
      setCallStartedAt(null)
      setResult({ type: 'error', message: err instanceof Error ? err.message : 'Unable to place the call.' })
    } finally {
      setLoading(false)
    }
  }

  // Media Streams needs Twilio, Gemini, a configured TTS provider, and a reachable PUBLIC_URL.
  // Vapi mode needs Twilio plus Vapi. Claim fields are checked when the button is clicked.
  const setupBlockers = callLaunchBlockers(health)
  const allReady = isCallLaunchReady(health)

  const hasClaim = !!(patientName || claimId || payerName || phone.trim())
  const progressStep = computeClaimProgressStep(activeCall, loading, hasClaim)
  const callLive = !!(activeCallId && (!activeCall || !isCallTerminal(activeCall.status)))

  return (
    <div className="max-w-[1280px] mx-auto space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">TPA Call Launch</h1>
          <p className="text-sm text-slate-500 mt-0.5">
            Pick a service sector, review sample questions, then call — dialogue stays inside TPA scope
          </p>
        </div>
        <span
          className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-semibold ${
            allReady ? 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200' : 'bg-amber-50 text-amber-800 ring-1 ring-amber-200'
          }`}
        >
          {allReady ? (
            <>
              <span className="w-2 h-2 rounded-full bg-emerald-500" />
              Ready to call
            </>
          ) : (
            <>
              <AlertCircle className="w-3.5 h-3.5" />
              Setup required
            </>
          )}
        </span>
      </div>

      {allReady && (health?.dialogue_mode === 'echo' || !health?.dialogue_mode) && (
        <div className="rounded-xl bg-sky-50/80 ring-1 ring-sky-200 px-4 py-3 text-sm text-sky-900">
          Echo Bot is active — say <strong>Hello</strong> and you should hear <strong>Hello Back</strong>
          {" "}(target ~500ms). Latency is saved on the call JSON as <code>echo_last</code>.
          Set <code>DIALOGUE_MODE=ai</code> in backend/.env for full claims AI.
        </div>
      )}

      {!allReady && (
        <div className="rounded-xl bg-amber-50/80 ring-1 ring-amber-200 px-4 py-3 text-sm text-amber-900">
          Calling stays off until this setup is complete:
          <ul className="mt-1 list-disc pl-5 space-y-0.5">
            {setupBlockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
          {health?.dialogue_mode === 'echo' ? (
            <span className="block mt-1 text-amber-800/90">Echo Bot mode is on — say “Hello” to hear “Hello Back”.</span>
          ) : null}
          {urlMessage && (
            <p className={`mt-2 text-xs ${urlMessage.type === 'success' ? 'text-amber-800' : 'text-red-700'}`}>
              {urlMessage.text}
            </p>
          )}
        </div>
      )}

      <TpaSectorPanel
        selectedSectorId={tpaSectorId}
        focusTopics={focusTopics}
        disabled={callLive || loading}
        onSectorChange={setTpaSectorId}
        onFocusTopicsChange={setFocusTopics}
      />

      <ScenarioSelectionPanel
        tpaProfile={tpaProfile}
        disabled={callLive || loading}
        onProfileChange={setTpaProfile}
      />

      <div className="rounded-2xl bg-white ring-1 ring-slate-200/80 shadow-[0_4px_24px_rgba(15,23,42,0.06)] overflow-hidden">
        <div className="px-5 py-4 bg-slate-50 border-b border-slate-200">
          <div className="flex flex-wrap items-center gap-2 mb-4">
            {hasClaim || tpaProfile ? (
              <>
                {patientName && (
                  <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-lg bg-white ring-1 ring-slate-200 text-xs font-semibold text-slate-800">
                    <User className="w-3 h-3 text-brand-600" />
                    {patientName}
                  </span>
                )}
                {claimId && (
                  <span className="px-3 py-1 rounded-lg bg-white ring-1 ring-slate-200 text-xs font-semibold text-slate-800">
                    Claim #{claimId}
                  </span>
                )}
                {payerName && (
                  <span className="px-3 py-1 rounded-lg bg-white ring-1 ring-slate-200 text-xs font-medium text-slate-600">
                    {payerName}
                  </span>
                )}
                <span className="px-3 py-1 rounded-lg bg-brand-600 text-xs font-semibold text-white">
                  {claimType}
                </span>
                {tpaSectorId && (
                  <span className="px-3 py-1 rounded-lg bg-sky-600 text-xs font-semibold text-white">
                    {tpaSectorId.replace(/_/g, ' ')}
                  </span>
                )}
                {focusTopics.length > 0 && (
                  <span className="px-3 py-1 rounded-lg bg-sky-50 ring-1 ring-sky-200 text-xs font-medium text-sky-800">
                    {focusTopics.length} focus topic{focusTopics.length === 1 ? '' : 's'}
                  </span>
                )}
                {(tpaProfile || activeCall?.tpa_executive_profile) && (
                  <span className="inline-flex items-center gap-1 px-3 py-1 rounded-lg bg-slate-50 ring-1 ring-slate-200 text-xs font-semibold text-slate-800">
                    <Users className="w-3 h-3" />
                    {activeCall?.tpa_executive_profile || tpaProfile}
                  </span>
                )}
              </>
            ) : (
              <span className="text-sm text-slate-500">Add claim details below to get started</span>
            )}
          </div>
          <HorizontalClaimProgress currentStep={progressStep} />
        </div>

        <div className="flex flex-col min-h-0">
          <div className="border-b border-slate-200/80 flex flex-col bg-white">
            <div className="px-5 py-4 border-b border-slate-100">
              <h2 className="text-sm font-bold text-slate-900">Claim Summary</h2>
              <p className="text-[11px] text-slate-500 mt-0.5">Patient and claim details for this follow-up</p>
            </div>

            <div className="px-5 py-4 space-y-3.5">
              <div>
                <p className="text-xs font-bold text-slate-900">Follow-up reason</p>
                <p className="text-[11px] text-slate-500 mt-0.5">
                  Pick one. The agent opens the call about this reason and stays on it.
                </p>
                <div className="mt-2.5 flex flex-wrap gap-2">
                  {CLAIM_TYPES.map((reason) => {
                    const selected = claimType === reason
                    return (
                      <button
                        key={reason}
                        type="button"
                        disabled={loading || callLive}
                        onClick={() => setClaimType(reason)}
                        className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition disabled:opacity-60 ${
                          selected
                            ? 'bg-brand-600 text-white shadow-sm ring-2 ring-sky-300/70'
                            : 'bg-white text-slate-700 ring-1 ring-slate-200 hover:ring-sky-300 hover:bg-sky-50'
                        }`}
                      >
                        {reason}
                      </button>
                    )
                  })}
                </div>
              </div>
              <div className="grid sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3.5">
                <Field
                  label="Insurance phone"
                  type="tel"
                  value={phone}
                  onChange={(e) => { setPhone(e.target.value); setPhoneHint(null) }}
                  placeholder="+91 98765 43210"
                  icon={<Phone className="w-3.5 h-3.5" />}
                  hint={phoneHint || undefined}
                  error={phoneHint?.includes('Invalid') ? phoneHint : undefined}
                />
                <Field label="Patient name" value={patientName} onChange={(e) => setPatientName(e.target.value)} icon={<User className="w-3.5 h-3.5" />} />
                <Field label="Claim #" value={claimId} onChange={(e) => setClaimId(e.target.value)} icon={<FileText className="w-3.5 h-3.5" />} />
                <Field label="Days out" type="number" min={1} value={claimAgeDays} onChange={(e) => setClaimAgeDays(e.target.value)} icon={<Calendar className="w-3.5 h-3.5" />} />
                <Field label="Insurance company" value={payerName} onChange={(e) => setPayerName(e.target.value)} />
                <Field label="Hospital" value={hospitalName} onChange={(e) => setHospitalName(e.target.value)} icon={<Building2 className="w-3.5 h-3.5" />} />
              </div>

              <button
                type="button"
                onClick={() => setShowExtra(!showExtra)}
                className="flex items-center gap-1 text-xs font-semibold text-brand-700 hover:text-brand-800"
              >
                {showExtra ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                Verification details
              </button>
              {showExtra && (
                <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3 pt-1">
                  <Field label="NPI" value={npi} onChange={(e) => setNpi(e.target.value)} />
                  <Field label="Policy #" value={policyNumber} onChange={(e) => setPolicyNumber(e.target.value)} />
                  <Field label="Representative" value={agentName} onChange={(e) => setAgentName(e.target.value)} />
                </div>
              )}
            </div>

            <div className="px-5 py-4 border-t border-slate-100 bg-slate-50/50 space-y-2">
              <Button
                onClick={handleCall}
                disabled={loading || callLive || !allReady}
                loading={loading}
                size="lg"
                className="w-full sm:w-auto sm:min-w-[240px] shadow-sm"
                icon={!loading ? <Phone className="w-4 h-4" /> : undefined}
              >
                {loading ? 'Calling insurance…' : callLive ? 'Call in progress…' : 'Call insurance company'}
              </Button>
              {result && (
                <div className={`flex gap-2 p-2.5 rounded-lg text-xs max-w-xl ${result.type === 'success' ? 'bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200' : 'bg-red-50 text-red-800 ring-1 ring-red-200'}`}>
                  {result.type === 'success' ? <CheckCircle2 className="w-4 h-4 shrink-0" /> : <AlertCircle className="w-4 h-4 shrink-0" />}
                  {result.message}
                </div>
              )}
            </div>
          </div>

          <div className="min-h-[420px]">
            <CallExperiencePanel
              agentName={agentName}
              patientName={patientName}
              hospitalName={hospitalName}
              claimId={claimId}
              claimType={claimType}
              payerName={payerName}
              phone={phone}
              activeCall={activeCall}
              callLive={callLive}
              initiating={loading}
              callStartedAt={callStartedAt}
              result={result}
              tpaProfile={activeCall?.tpa_executive_profile || tpaProfile}
            />
          </div>
        </div>
      </div>
    </div>
  )
}
