import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { AlertCircle, Sparkles } from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { FloatingInput } from '../components/ui/Input'
import Button from '../components/ui/Button'
import AuthLayout from '../components/AuthLayout'

export default function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await login(email, password)
      navigate('/dashboard')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <AuthLayout>
      <div className="w-full max-w-md opacity-0 animate-fade-up" style={{ animationFillMode: 'forwards' }}>
        <div className="rounded-xl bg-white border border-slate-200 shadow-sm overflow-hidden">
          <div className="h-1 bg-brand-800" />
          <div className="p-8">
            <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-brand-100 text-brand-800 text-[10px] font-bold uppercase tracking-wider mb-4">
              <Sparkles className="w-3 h-3" /> Hospital RCM
            </span>
            <h1 className="text-2xl font-bold text-slate-900 mb-1 tracking-tight">Welcome back</h1>
            <p className="text-slate-500 text-sm mb-8">Log in to launch payer claims follow-up calls</p>

            <form onSubmit={handleSubmit} className="space-y-5">
              <FloatingInput
                label="Email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
              />
              <FloatingInput
                label="Password"
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
              />

              {error && (
                <div className="flex items-start gap-2 p-3 rounded-xl bg-red-50 text-red-800 border border-red-200 text-sm animate-scale-in">
                  <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
                  {error}
                </div>
              )}

              <Button type="submit" loading={loading} size="lg" className="w-full shadow-md">
                Log in
              </Button>
            </form>

            <p className="text-center text-sm text-slate-500 mt-6">
              Don&apos;t have an account?{' '}
              <Link to="/signup" className="text-brand-700 font-semibold hover:text-brand-800 transition-colors">
                Sign up
              </Link>
            </p>
          </div>
        </div>
      </div>
    </AuthLayout>
  )
}
