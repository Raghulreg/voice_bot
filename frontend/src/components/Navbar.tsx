import { Link, useNavigate } from 'react-router-dom'
import { Phone, LayoutDashboard, History, Circle, LogOut } from 'lucide-react'
import type { HealthStatus } from '../api'
import { useAuth } from '../context/AuthContext'

interface Props {
  activeTab: 'dashboard' | 'history'
  onTabChange: (tab: 'dashboard' | 'history') => void
  health: HealthStatus | null
}

export default function Navbar({ activeTab, onTabChange, health }: Props) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const provider = health?.call_provider || 'media_streams'
  const allConfigured =
    provider === 'vapi'
      ? !!(health?.twilio_configured && health?.vapi_configured)
      : !!(
          health?.twilio_configured &&
          health?.gemini_configured &&
          health?.tts_configured &&
          health?.public_url_ready
        )

  const tabs = [
    { id: 'dashboard' as const, label: 'Launch Call', icon: LayoutDashboard },
    { id: 'history' as const, label: 'Call History', icon: History },
  ]

  const handleLogout = () => {
    logout()
    navigate('/')
  }

  return (
    <nav className="fixed top-0 left-0 right-0 z-50 bg-white/95 backdrop-blur-lg border-b border-slate-200 shadow-sm">
      <div className="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
        <Link to="/dashboard" className="flex items-center gap-2.5">
          <div className="w-9 h-9 bg-brand-700 rounded-lg flex items-center justify-center shadow-sm">
            <Phone className="w-5 h-5 text-white" />
          </div>
          <span className="font-bold text-lg text-navy-900 tracking-tight hidden sm:inline">VoiceCall AI</span>
        </Link>

        <div className="hidden md:flex items-center gap-1">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => onTabChange(t.id)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                activeTab === t.id
                  ? 'bg-brand-50 text-brand-800 border border-brand-100'
                  : 'text-slate-600 hover:bg-slate-50 hover:text-navy-900'
              }`}
            >
              <t.icon className="w-4 h-4" />
              {t.label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-3">
          <div className="hidden sm:flex items-center gap-2 text-xs">
            <Circle
              className={`w-2.5 h-2.5 ${allConfigured ? 'fill-emerald-500 text-emerald-500' : 'fill-amber-400 text-amber-400'}`}
            />
            <span className="text-slate-500">
              {allConfigured ? 'Ready' : 'Configure .env'}
            </span>
          </div>
          <span className="text-sm text-slate-600 hidden lg:inline">{user?.name}</span>
          <button
            onClick={handleLogout}
            className="flex items-center gap-1.5 px-3 py-2 text-sm text-slate-600 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
            title="Log out"
          >
            <LogOut className="w-4 h-4" />
            <span className="hidden sm:inline">Logout</span>
          </button>
        </div>
      </div>
    </nav>
  )
}
