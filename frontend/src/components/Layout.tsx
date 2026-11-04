import { useRef, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  BarChart3, CalendarClock, Clapperboard, Download, Film, FolderUp, HeartPulse, LayoutGrid, LineChart, Loader2, MoreHorizontal, Plus,
  Settings as SettingsIcon, Tv,
} from 'lucide-react'
import { useInstallPrompt } from '@/lib/pwa'
import { cn } from '@/lib/utils'

const GROUPS = [
  {
    label: 'Make',
    items: [
      { to: '/', label: 'Jobs', icon: LayoutGrid, end: true },
      { to: '/new', label: 'New job', icon: Plus },
    ],
  },
  {
    label: 'Publish',
    items: [
      { to: '/schedule', label: 'Schedule', icon: CalendarClock },
      { to: '/batches', label: 'Batches', icon: FolderUp },
      { to: '/dashboard', label: 'Dashboard', icon: LineChart },
      { to: '/tracker', label: 'Tracker', icon: BarChart3 },
      { to: '/channels', label: 'Accounts', icon: Tv },
    ],
  },
  {
    label: 'App',
    items: [
      { to: '/settings', label: 'Settings', icon: SettingsIcon },
      { to: '/system', label: 'System', icon: HeartPulse },
    ],
  },
]
// phone bottom bar (Phase 14): Jobs, Gallery (latest episode), Schedule, Dashboard, More
const MOBILE_MAIN = [
  { to: '/', label: 'Jobs', icon: LayoutGrid, end: true },
  { to: '/gallery', label: 'Gallery', icon: Film },
  { to: '/schedule', label: 'Schedule', icon: CalendarClock },
  { to: '/dashboard', label: 'Stats', icon: LineChart },
  { to: '/more', label: 'More', icon: MoreHorizontal },
]
const MORE_PATHS = ['/more', '/new', '/batches', '/tracker', '/channels', '/settings', '/system']

/** Pull down at the top of a page to refresh its data (phones). */
function usePullToRefresh() {
  const qc = useQueryClient()
  const startY = useRef<number | null>(null)
  const [pull, setPull] = useState(0)
  const [busy, setBusy] = useState(false)
  return {
    pull, busy,
    handlers: {
      onTouchStart: (e: React.TouchEvent) => {
        startY.current = window.scrollY <= 0 ? e.touches[0].clientY : null
      },
      onTouchMove: (e: React.TouchEvent) => {
        if (startY.current == null) return
        const d = e.touches[0].clientY - startY.current
        setPull(d > 0 ? Math.min(90, d / 2) : 0)
      },
      onTouchEnd: async () => {
        if (pull >= 70) {
          setBusy(true)
          await qc.invalidateQueries()
          setBusy(false)
        }
        startY.current = null
        setPull(0)
      },
    },
  }
}

function Brand() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground">
        <Clapperboard className="size-4.5" strokeWidth={2.5} />
      </div>
      <div className="leading-tight">
        <div className="font-semibold tracking-tight">ClipForge</div>
        <div className="text-[11px] text-muted-foreground">podcast → shorts</div>
      </div>
    </div>
  )
}

export default function Layout() {
  const { pathname } = useLocation()
  const wide = pathname.includes('/edit') || pathname.startsWith('/schedule')
  const moreActive = MORE_PATHS.some((m) => pathname.startsWith(m))
  const ptr = usePullToRefresh()
  const { canInstall, install } = useInstallPrompt()
  return (
    <div className="min-h-dvh bg-background">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col border-r border-sidebar-border bg-sidebar px-3 py-5 md:flex">
        <div className="px-2"><Brand /></div>
        <nav className="mt-6 flex flex-col gap-5 overflow-y-auto">
          {GROUPS.map((g) => (
            <div key={g.label} className="flex flex-col gap-1">
              <div className="px-3 text-[10px] font-semibold tracking-wider text-muted-foreground/70 uppercase">{g.label}</div>
              {g.items.map(({ to, label, icon: Icon, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  className={({ isActive }) =>
                    cn('flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground',
                      isActive && 'bg-sidebar-accent font-medium text-foreground')
                  }
                >
                  <Icon className="size-4" />
                  {label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="mt-auto px-3 pt-4 text-[11px] leading-relaxed text-muted-foreground">100% free &amp; local.<br />No watermark, ever.</div>
      </aside>

      <header className="sticky top-0 z-30 flex items-center justify-between border-b bg-background/85 px-4 py-3 backdrop-blur md:hidden">
        <Brand />
        {canInstall && (
          <button onClick={install} className="flex h-9 items-center gap-1.5 rounded-lg bg-primary px-3 text-sm font-medium text-primary-foreground">
            <Download className="size-4" /> Install app
          </button>
        )}
      </header>

      {(ptr.pull > 0 || ptr.busy) && (
        <div className="fixed inset-x-0 top-14 z-40 flex justify-center md:hidden" style={{ transform: `translateY(${ptr.pull / 3}px)` }}>
          <div className="rounded-full border bg-card p-2 shadow"><Loader2 className={cn('size-4', (ptr.busy || ptr.pull >= 70) && 'animate-spin')} /></div>
        </div>
      )}
      <main className="pb-24 md:pb-10 md:pl-56" {...ptr.handlers}>
        <div className={cn('mx-auto px-4 py-6 md:px-8 md:py-8', wide ? 'max-w-[1500px]' : 'max-w-6xl')}>
          <Outlet />
        </div>
      </main>

      <nav className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden">
        {MOBILE_MAIN.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end}
            className={({ isActive }) => cn('flex min-h-14 flex-col items-center justify-center gap-1 py-2 text-[11px] text-muted-foreground',
              (isActive || (to === '/more' && moreActive) || (to === '/gallery' && pathname.includes('/clips'))) && 'text-primary')}>
            <Icon className="size-5" />
            {label}
          </NavLink>
        ))}
      </nav>
    </div>
  )
}
