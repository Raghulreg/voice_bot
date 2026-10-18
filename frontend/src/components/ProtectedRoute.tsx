import { Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { KpiSkeleton } from './ui/EmptyState'

export default function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading } = useAuth()

  if (isLoading) {
    return (
      <div className="min-h-screen bg-surface p-8">
        <div className="max-w-4xl mx-auto space-y-6">
          <div className="skeleton h-8 w-48 rounded-lg" />
          <KpiSkeleton count={4} />
        </div>
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/login" replace />
  }

  return <>{children}</>
}
