import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { AlertCircle, Sparkles } from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { FloatingInput } from '../components/ui/Input'
import Button from '../components/ui/Button'
import AuthLayout from '../components/AuthLayout'

export default function SignupPage() {
  const { signup } = useAuth()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await signup(name, email, password)
      navigate('/dashboard')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign up failed')
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
            <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-100 text-slate-800 text-[10px] font-bold uppercase tracking-wider mb-4">
              <Sparkles className="w-3 h-3" /> Get started free
            </span>
            <h1 className="text-2xl font-bold text-slate-900 mb-1 tracking-tight">Create account</h1>
            <p className="text-slate-500 text-sm mb-8">Start managing hospital-to-TPA claims calls today</p>

            <form onSubmit={handleSubmit} className="space-y-5">
              <FloatingInput
                label="Full name"
                type="text"
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Rahul Sharma"
              />
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
                minLength={6}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Min. 6 characters"
                hint="Minimum 6 characters"
              />

              {error && (
                <div className="flex items-start gap-2 p-3 rounded-xl bg-red-50 text-red-800 border border-red-200 text-sm animate-scale-in">
                  <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
                  {error}
                </div>
              )}

              <Button type="submit" loading={loading} size="lg" className="w-full shadow-md">
                Create account
              </Button>
            </form>

            <p className="text-center text-sm text-slate-500 mt-6">
              Already have an account?{' '}
              <Link to="/login" className="text-brand-700 font-semibold hover:text-brand-800 transition-colors">
                Log in
              </Link>
            </p>
          </div>
        </div>
      </div>
    </AuthLayout>
  )
}
