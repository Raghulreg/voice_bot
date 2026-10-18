import { useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  Phone,
  LayoutDashboard,
  History,
  ChevronLeft,
  ChevronRight,
  Bell,
  Menu,
  X,
  Circle,
  SlidersHorizontal,
} from 'lucide-react'
import type { HealthStatus } from '../api'
import { useAuth } from '../context/AuthContext'

export type DashboardTab = 'overview' | 'launch' | 'history' | 'quality'

const navItems = [
  { id: 'overview' as const, label: 'Dashboard', icon: LayoutDashboard },
  { id: 'launch' as const, label: 'Follow Up', icon: Phone },
  { id: 'history' as const, label: 'Call Records', icon: History },
  { id: 'quality' as const, label: 'Voice & Quality', icon: SlidersHorizontal },
]

interface AppShellProps {
  activeTab: DashboardTab
  onTabChange: (tab: DashboardTab) => void
  health: HealthStatus | null
  children: ReactNode
}

export default function AppShell({ activeTab, onTabChange, health, children }: AppShellProps) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)

  const provider = health?.call_provider || 'media_streams'
  const ready =
    provider === 'vapi'
      ? !!(health?.twilio_configured && health?.vapi_configured)
      : !!(
          health?.twilio_configured &&
          health?.gemini_configured &&
          health?.tts_configured &&
          health?.public_url_ready
        )

  const activeItem = navItems.find((n) => n.id === activeTab)

  return (
    <div className="dashboard-bg min-h-screen">
      {mobileOpen && (
        <button
          className="fixed inset-0 z-30 bg-navy-950/40 backdrop-blur-sm lg:hidden animate-fade-in"
          onClick={() => setMobileOpen(false)}
          aria-label="Close menu"
        />
      )}

      {/* Gradient sidebar */}
      <aside
        className={`app-sidebar fixed top-0 left-0 z-40 h-full flex flex-col transition-all duration-300 ease-out shadow-sidebar ${
          collapsed ? 'w-[68px]' : 'w-[240px]'
        } ${mobileOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'}`}
      >
        <div className={`h-16 flex items-center shrink-0 border-b border-white/10 ${collapsed ? 'justify-center' : 'px-5 gap-3'}`}>
          <div className="w-9 h-9 bg-white/15 backdrop-blur rounded-xl flex items-center justify-center shrink-0 ring-1 ring-white/20">
            <Phone className="w-4 h-4 text-white" strokeWidth={2} />
          </div>
          {!collapsed && (
            <div>
              <span className="font-semibold text-sm text-white tracking-tight block">ClaimsVoice</span>
              <span className="text-[10px] sidebar-brand-sub">Claims Follow-Up</span>
            </div>
          )}
        </div>

        <nav className="flex-1 px-3 py-5 space-y-1" aria-label="Main navigation">
          {navItems.map((item) => {
            const active = activeTab === item.id
            return (
              <button
                key={item.id}
                onClick={() => { onTabChange(item.id); setMobileOpen(false) }}
                title={collapsed ? item.label : undefined}
                className={`sidebar-nav-item relative w-full flex items-center gap-3 rounded-xl text-left transition-all duration-200 ${
                  collapsed ? 'justify-center p-3' : 'px-3.5 py-2.5'
                } ${active ? 'nav-item-active text-white' : ''}`}
                aria-current={active ? 'page' : undefined}
              >
                <item.icon className="w-[18px] h-[18px] shrink-0" strokeWidth={active ? 2 : 1.75} />
                {!collapsed && <span className="text-sm font-medium">{item.label}</span>}
              </button>
            )
          })}
        </nav>

        {!collapsed && (
          <div className="mx-3 mb-4 p-4 rounded-xl bg-white/10 backdrop-blur border border-white/10">
            <div className="flex items-center gap-2 mb-1">
              <Circle className={`w-2 h-2 ${ready ? 'fill-emerald-400 text-emerald-400' : 'fill-amber-400 text-amber-400'}`} />
              <span className="text-xs font-medium text-white">{ready ? 'Ready to call' : 'Setup required'}</span>
            </div>
            <p className="text-[11px] sidebar-status-copy leading-relaxed">
              {ready ? 'All systems operational for follow-up calls.' : 'Contact your administrator.'}
            </p>
          </div>
        )}

        <div className="p-3 border-t border-white/10">
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="hidden lg:flex w-full items-center justify-center rounded-lg p-2.5 text-brand-100/80 hover:bg-white/10 hover:text-white transition-colors"
            aria-label={collapsed ? 'Expand' : 'Collapse'}
          >
            {collapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronLeft className="w-4 h-4" />}
          </button>
        </div>
      </aside>

      <div className={`transition-all duration-300 ease-out ${collapsed ? 'lg:pl-[68px]' : 'lg:pl-[240px]'}`}>
        <header className="sticky top-0 z-20 glass-header h-16 flex items-center px-6 lg:px-8 gap-4">
          <button
            className="lg:hidden p-2 rounded-xl text-content-muted hover:bg-white/80 transition-colors"
            onClick={() => setMobileOpen(true)}
            aria-label="Menu"
          >
            {mobileOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
          </button>

          <nav aria-label="Breadcrumb" className="flex items-center gap-2 text-sm text-content-muted min-w-0">
            <Link to="/dashboard" className="hover:text-brand-700 transition-colors shrink-0">
              Home
            </Link>
            <ChevronRight className="w-3.5 h-3.5 shrink-0 text-content-subtle" />
            <span className="text-content-primary font-semibold truncate">{activeItem?.label}</span>
          </nav>

          <div className="flex-1" />

          <div className="flex items-center gap-2 shrink-0">
            <button className="p-2.5 rounded-xl text-content-subtle hover:bg-white/80 hover:text-content-secondary transition-all duration-200" aria-label="Notifications">
              <Bell className="w-4 h-4" strokeWidth={1.75} />
            </button>
            <button
              onClick={() => { logout(); navigate('/') }}
              className="flex items-center gap-2.5 pl-1.5 pr-3 py-1.5 rounded-xl hover:bg-white/80 transition-all duration-200"
            >
              <div className="w-8 h-8 rounded-full bg-brand-800 flex items-center justify-center text-xs font-semibold text-white">
                {user?.name?.charAt(0)?.toUpperCase() || 'U'}
              </div>
              <span className="hidden md:block text-sm font-medium text-content-secondary max-w-[120px] truncate">
                {user?.name?.split(' ')[0]}
              </span>
            </button>
          </div>
        </header>

        <main className="px-5 py-5 lg:px-7 lg:py-6 max-w-[1500px] mx-auto">
          <div key={activeTab} className="page-enter">
            {children}
          </div>
        </main>
      </div>
    </div>
  )
}
