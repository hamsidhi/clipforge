import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  BarChart3, ChevronRight, Download, FolderUp, HeartPulse, LogOut, Plus, Settings as SettingsIcon, Smartphone, Tv,
} from 'lucide-react'
import { request } from '@/api/client'
import { PageHeader } from '@/components/common'
import { useInstallPrompt } from '@/lib/pwa'

const LINKS = [
  { to: '/new', label: 'New job', icon: Plus },
  { to: '/channels', label: 'Accounts', icon: Tv },
  { to: '/batches', label: 'Batches & kits', icon: FolderUp },
  { to: '/tracker', label: 'Tracker', icon: BarChart3 },
  { to: '/settings', label: 'Settings', icon: SettingsIcon },
  { to: '/settings#remote', label: 'Phone, Telegram & security', icon: Smartphone },
  { to: '/system', label: 'System check', icon: HeartPulse },
]

/** "More" tab of the phone's bottom bar. */
export default function More() {
  const { canInstall, install } = useInstallPrompt()
  const { data: auth } = useQuery({ queryKey: ['auth'], queryFn: () => request<{ local: boolean; device: string | null }>('/api/auth/status') })
  const logout = async () => {
    await request('/api/auth/logout', { method: 'POST' })
    window.location.assign('/login')
  }
  const row = 'flex min-h-14 items-center gap-3 px-4 text-sm hover:bg-muted/40'
  return (
    <>
      <PageHeader title="More" />
      <div className="divide-y overflow-hidden rounded-xl border">
        {LINKS.map(({ to, label, icon: Icon }) => (
          <Link key={to} to={to} className={row}><Icon className="size-5 text-muted-foreground" /> <span className="flex-1">{label}</span> <ChevronRight className="size-4 text-muted-foreground" /></Link>
        ))}
        {canInstall && (
          <button className={`${row} w-full text-left`} onClick={install}><Download className="size-5 text-primary" /> Install ClipForge on this phone</button>
        )}
        {auth && !auth.local && (
          <button className={`${row} w-full text-left text-destructive`} onClick={logout}><LogOut className="size-5" /> Log out{auth.device ? ` (${auth.device})` : ''}</button>
        )}
      </div>
    </>
  )
}
