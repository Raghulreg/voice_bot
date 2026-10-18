import { Link } from 'react-router-dom'
import { Phone } from 'lucide-react'
import type { ReactNode } from 'react'

interface AuthLayoutProps {
  children: ReactNode
}

export default function AuthLayout({ children }: AuthLayoutProps) {
  return (
    <div className="min-h-screen flex flex-col relative overflow-hidden bg-slate-50">
      <div className="absolute inset-0 bg-gradient-to-br from-brand-50 via-white to-accent-50 pointer-events-none" />

      <div className="relative p-6">
        <Link to="/" className="inline-flex items-center gap-2.5 group">
          <div className="w-9 h-9 bg-brand-800 rounded-md flex items-center justify-center">
            <Phone className="w-5 h-5 text-white" strokeWidth={2} />
          </div>
          <span className="font-semibold text-sm text-slate-900">
            ClaimsVoice
          </span>
        </Link>
      </div>

      <div className="relative flex-1 flex items-center justify-center px-6 pb-16">
        {children}
      </div>
    </div>
  )
}
