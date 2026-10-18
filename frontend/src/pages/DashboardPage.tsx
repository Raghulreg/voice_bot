import { useCallback, useEffect, useState } from 'react'
import AppShell, { type DashboardTab } from '../components/AppShell'
import CallDashboard from '../components/CallDashboard'
import CallHistory from '../components/CallHistory'
import ConversationQualityPanel from '../components/ConversationQualityPanel'
import OverviewPanel from '../components/OverviewPanel'
import { fetchHealth, type HealthStatus } from '../api'

export default function DashboardPage() {
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [activeTab, setActiveTab] = useState<DashboardTab>('launch')

  const refreshHealth = useCallback(() => {
    fetchHealth().then(setHealth).catch(() => undefined)
  }, [])

  useEffect(() => {
    let cancelled = false
    let timer = 0
    const load = () => {
      fetchHealth()
        .then((next) => {
          if (!cancelled) setHealth(next)
        })
        .catch(() => {
          if (!cancelled) timer = window.setTimeout(load, 1500)
        })
    }
    load()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [])

  const goToFollowUp = () => setActiveTab('launch')

  return (
    <AppShell activeTab={activeTab} onTabChange={setActiveTab} health={health}>
      {activeTab === 'overview' && <OverviewPanel onNavigateLaunch={goToFollowUp} />}
      {activeTab === 'launch' && (
        <CallDashboard health={health} onHealthRefresh={refreshHealth} />
      )}
      {activeTab === 'history' && <CallHistory />}
      {activeTab === 'quality' && <ConversationQualityPanel />}
    </AppShell>
  )
}
