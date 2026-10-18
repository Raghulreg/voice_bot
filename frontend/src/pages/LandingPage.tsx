import {
  Phone,
  FileCheck,
  ClipboardList,
  Shield,
  ArrowRight,
  CheckCircle2,
  Building2,
  Clock,
  Sparkles,
  Zap,
} from 'lucide-react'
import PublicNavbar from '../components/PublicNavbar'
import DemoCall from '../components/DemoCall'
import Button from '../components/ui/Button'
import ScrollReveal from '../components/ScrollReveal'

const statCard = 'bg-white border-brand-100 shadow-sm'
const stats = [
  { value: '85%', label: 'Follow-up coverage', valueClass: 'text-brand-700' },
  { value: '7', label: 'Steps per call', valueClass: 'text-accent-700' },
  { value: '20+', label: 'Payer scenarios', valueClass: 'text-brand-700' },
  { value: '24/7', label: 'Availability', valueClass: 'text-accent-700' },
]

const features = [
  {
    icon: Phone,
    title: 'Insurance follow-up calls',
    desc: 'Outbound calls to payer helplines from your claims queue.',
    iconBg: 'bg-brand-50 ring-brand-100',
    iconColor: 'text-brand-700',
    card: 'bg-white border-brand-100 hover:border-brand-200',
  },
  {
    icon: ClipboardList,
    title: 'Claim status & denials',
    desc: 'Denial codes, pending documents, and payment timelines.',
    iconBg: 'bg-rose-50 ring-rose-100',
    iconColor: 'text-rose-700',
    card: 'bg-white border-brand-100 hover:border-brand-200',
  },
  {
    icon: FileCheck,
    title: 'Structured outcomes',
    desc: 'Every call logged with resolution status and next steps.',
    iconBg: 'bg-accent-50 ring-accent-100',
    iconColor: 'text-accent-700',
    card: 'bg-white border-brand-100 hover:border-brand-200',
  },
  {
    icon: Building2,
    title: 'Built for hospital RCM',
    desc: 'For teams managing TPA and insurance follow-up.',
    iconBg: 'bg-sky-50 ring-sky-100',
    iconColor: 'text-sky-700',
    card: 'bg-white border-brand-100 hover:border-brand-200',
  },
  {
    icon: Shield,
    title: 'Enterprise security',
    desc: 'HIPAA-ready with role-based access for hospital staff.',
    iconBg: 'bg-emerald-50 ring-emerald-100',
    iconColor: 'text-emerald-700',
    card: 'bg-white border-brand-100 hover:border-brand-200',
  },
  {
    icon: Clock,
    title: 'Faster resolution',
    desc: 'Reduce days in AR on repetitive status inquiries.',
    iconBg: 'bg-amber-50 ring-amber-100',
    iconColor: 'text-amber-800',
    card: 'bg-white border-brand-100 hover:border-brand-200',
  },
]

const steps = [
  { n: '1', title: 'Select a claim', desc: 'Pick an outstanding claim from your queue' },
  { n: '2', title: 'Review details', desc: 'Confirm patient, payer, and claim info' },
  { n: '3', title: 'Follow up by phone', desc: 'Connect with the payer representative' },
  { n: '4', title: 'Record outcome', desc: 'Log status, documents, and next actions' },
]

const featureVariants = ['up', 'left', 'right', 'up', 'right', 'left'] as const

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-brand-50">
      <PublicNavbar />

      {/* Hero */}
      <section className="relative pt-20 pb-12 px-5 lg:px-6 overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-br from-brand-100/80 via-white to-accent-50/70 pointer-events-none" />

        <div className="relative max-w-6xl mx-auto">
          <div className="grid lg:grid-cols-2 gap-10 lg:gap-14 items-center">
            <div className="opacity-0 animate-fade-up" style={{ animationFillMode: 'forwards' }}>
              <span className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md bg-accent-50 border border-accent-200 text-xs font-semibold text-accent-800 uppercase tracking-wider mb-4">
                <Sparkles className="w-3.5 h-3.5 text-accent-600" />
                Hospital claims follow-up
              </span>
              <h1 className="text-3xl sm:text-4xl lg:text-[2.65rem] font-bold text-slate-900 leading-[1.12] tracking-tight mb-4">
                Follow up on insurance claims{' '}
                <span className="text-brand-600">
                  without the hold times
                </span>
              </h1>
              <p className="text-slate-600 text-[15px] leading-relaxed mb-6 max-w-md">
                Contact payers, confirm claim status, capture denial details, and document outcomes — in one workflow.
              </p>
              <div className="flex flex-wrap gap-3 mb-5">
                <Button variant="primary" size="lg" to="/signup" icon={<ArrowRight className="w-4 h-4" />}>
                  Start free trial
                </Button>
                <Button variant="secondary" size="lg" to="/login">Sign in</Button>
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-2">
                {[
                  { t: 'Hospital RCM teams' },
                  { t: 'HIPAA ready' },
                  { t: 'No manual dialing' },
                ].map(({ t }) => (
                  <span key={t} className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
                    <span className="w-5 h-5 rounded-full bg-accent-50 flex items-center justify-center">
                      <CheckCircle2 className="w-3 h-3 text-accent-700" />
                    </span>
                    {t}
                  </span>
                ))}
              </div>
            </div>

            <div id="demo" className="relative opacity-0 animate-fade-up stagger-2" style={{ animationFillMode: 'forwards' }}>
              <div className="absolute -inset-4 pointer-events-none" />
              <DemoCall />
            </div>
          </div>

          {/* Stats row */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-12">
            {stats.map((s, i) => (
              <ScrollReveal key={s.label} variant="scale" delay={i * 100} duration={800}>
                <div
                  className={`rounded-xl px-4 py-4 border text-center sm:text-left ${statCard}`}
                >
                  <p className={`text-2xl font-semibold tabular-nums ${s.valueClass}`}>{s.value}</p>
                  <p className="text-xs font-medium mt-0.5 text-slate-500">{s.label}</p>
                </div>
              </ScrollReveal>
            ))}
          </div>
        </div>
      </section>

      {/* Features */}
      <section className="py-14 px-5 lg:px-6 bg-white border-t border-brand-100">
        <div className="max-w-6xl mx-auto">
          <ScrollReveal variant="blur-up" duration={1000}>
            <div className="text-center mb-10">
              <span className="inline-flex items-center gap-1.5 text-xs font-bold text-brand-700 uppercase tracking-wider mb-2">
                <Zap className="w-3.5 h-3.5 text-accent-600" /> Platform features
              </span>
              <h2 className="text-2xl font-bold text-slate-900 mb-2">Everything your RCM team needs</h2>
              <p className="text-sm text-slate-500 max-w-md mx-auto">From claim selection to a documented outcome, in one workflow.</p>
            </div>
          </ScrollReveal>
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {features.map((f, i) => (
              <ScrollReveal
                key={f.title}
                variant={featureVariants[i % featureVariants.length]}
                delay={(i % 3) * 120}
                duration={850}
              >
                <div
                  className={`rounded-2xl p-5 border h-full transition-all duration-300 hover:-translate-y-1.5 hover:shadow-xl ${f.card}`}
                >
                  <div className={`w-10 h-10 rounded-xl ring-1 flex items-center justify-center mb-3 ${f.iconBg}`}>
                    <f.icon className={`w-5 h-5 ${f.iconColor}`} strokeWidth={1.75} />
                  </div>
                  <h3 className="text-sm font-bold text-slate-900 mb-1.5">{f.title}</h3>
                  <p className="text-xs text-slate-600 leading-relaxed">{f.desc}</p>
                </div>
              </ScrollReveal>
            ))}
          </div>
        </div>
      </section>

      {/* How it works */}
      <section className="py-14 px-5 lg:px-6 bg-brand-950 text-white relative overflow-hidden">
        <div className="relative max-w-6xl mx-auto">
          <ScrollReveal variant="up" duration={900}>
            <div className="text-center mb-10">
              <h2 className="text-2xl font-bold text-white mb-2">How your team uses ClaimsVoice</h2>
              <p className="text-sm text-brand-100/80">Four steps from claim queue to documented outcome</p>
            </div>
          </ScrollReveal>
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {steps.map((s, i) => (
              <ScrollReveal key={s.n} variant="up" delay={i * 140} duration={900}>
                <div className="relative h-full">
                  {i < steps.length - 1 && (
                    <div className="hidden lg:block absolute top-8 -right-2 w-4 h-0.5 bg-gradient-to-r from-white/30 to-transparent z-10" />
                  )}
                  <div className="rounded-xl p-5 border border-white/10 bg-white h-full">
                    <span className="inline-flex w-8 h-8 rounded-md bg-brand-800 text-white text-sm font-semibold items-center justify-center mb-3">
                      {s.n}
                    </span>
                    <p className="text-sm font-bold text-slate-900">{s.title}</p>
                    <p className="text-xs text-slate-600 mt-1.5 leading-relaxed">{s.desc}</p>
                  </div>
                </div>
              </ScrollReveal>
            ))}
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="px-5 lg:px-6 py-12">
        <ScrollReveal variant="scale" duration={1000}>
          <div className="max-w-6xl mx-auto rounded-xl bg-brand-900 px-6 py-8 sm:px-10 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-5">
            <div>
              <h2 className="text-xl font-bold text-white">Ready to streamline payer follow-up?</h2>
              <p className="text-sm text-brand-50/90 mt-1">Set up your hospital account in minutes.</p>
            </div>
            <Button
              size="lg"
              variant="secondary"
              to="/signup"
              className="shrink-0 !bg-white !text-brand-800 hover:!bg-brand-50 !border-white font-semibold"
              icon={<ArrowRight className="w-4 h-4" />}
            >
              Get started
            </Button>
          </div>
        </ScrollReveal>
      </section>

      <ScrollReveal variant="up" delay={80} duration={700}>
        <footer className="py-6 px-5 lg:px-6 border-t border-slate-200 bg-white">
          <div className="max-w-6xl mx-auto flex flex-col sm:flex-row justify-between items-center gap-3 text-xs text-slate-500">
            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-md bg-brand-800 flex items-center justify-center">
                <Phone className="w-3.5 h-3.5 text-white" />
              </div>
              <span className="font-bold text-slate-800">ClaimsVoice</span>
            </div>
            <p>&copy; 2026 ClaimsVoice · Hospital RCM follow-up</p>
          </div>
        </footer>
      </ScrollReveal>
    </div>
  )
}
