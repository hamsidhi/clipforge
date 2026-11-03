import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { BarChart3, Clapperboard, HeartPulse, LayoutGrid, Plus, Settings as SettingsIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

const NAV = [
  { to: '/', label: 'Jobs', icon: LayoutGrid, end: true },
  { to: '/new', label: 'New job', icon: Plus },
  { to: '/tracker', label: 'Tracker', icon: BarChart3 },
  { to: '/settings', label: 'Settings', icon: SettingsIcon },
  { to: '/system', label: 'System', icon: HeartPulse },
]

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
  const wide = pathname.includes('/edit')
  return (
    <div className="min-h-dvh bg-background">
      {/* desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col border-r border-sidebar-border bg-sidebar px-3 py-5 md:flex">
        <div className="px-2">
          <Brand />
        </div>
        <nav className="mt-8 flex flex-col gap-1">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground',
                  isActive && 'bg-sidebar-accent font-medium text-foreground',
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto px-3 text-[11px] leading-relaxed text-muted-foreground">
          100% free &amp; local.
          <br />
          No watermark, ever.
        </div>
      </aside>

      {/* mobile top bar */}
      <header className="sticky top-0 z-30 flex items-center justify-between border-b bg-background/85 px-4 py-3 backdrop-blur md:hidden">
        <Brand />
      </header>

      <main className={cn('md:pl-56', 'pb-24 md:pb-10')}>
        <div className={cn('mx-auto px-4 py-6 md:px-8 md:py-8', wide ? 'max-w-[1500px]' : 'max-w-6xl')}>
          <Outlet />
        </div>
      </main>

      {/* mobile bottom nav */}
      <nav className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden">
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              cn('flex flex-col items-center gap-1 py-2.5 text-[10px] text-muted-foreground', isActive && 'text-primary')
            }
          >
            <Icon className="size-5" />
            {label}
          </NavLink>
        ))}
      </nav>
    </div>
  )
}
