import { Link } from 'react-router-dom'
import { Phone } from 'lucide-react'
import Button from './ui/Button'

export default function PublicNavbar() {
  return (
    <nav className="fixed top-0 left-0 right-0 z-50 bg-white/95 backdrop-blur-md border-b border-slate-200">
      <div className="max-w-6xl mx-auto px-5 lg:px-6 h-14 flex items-center justify-between">
        <Link to="/" className="flex items-center gap-2.5 group">
          <div className="w-8 h-8 bg-brand-800 rounded-md flex items-center justify-center">
            <Phone className="w-4 h-4 text-white" strokeWidth={2} />
          </div>
          <span className="font-semibold text-sm text-slate-900 tracking-tight">
            ClaimsVoice
          </span>
        </Link>

        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" to="/login">Login</Button>
          <Button variant="primary" size="sm" to="/signup">Sign up</Button>
        </div>
      </div>
    </nav>
  )
}
